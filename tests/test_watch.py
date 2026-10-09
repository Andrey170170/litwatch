"""Triage, the keyword audit and grading against disposable PostgreSQL, with scripted models."""

import json
from types import SimpleNamespace

import pytest

from litwatch import screening, watch
from litwatch.config import Config
from litwatch.core import Grade, Status
from litwatch.jobs import nightly
from litwatch.jobs import watch as watch_jobs
from litwatch.jobs.context import model_run
from litwatch.llm import Gateway, ProviderError
from litwatch.store import papers as papers_store
from litwatch.store import runs, verdicts
from litwatch.store import topics as topics_store
from litwatch.store import triage as triage_store
from tests.conftest import FAITH, TOPICS, paper
from tests.fakes import ScriptedProvider

ON_TOPIC = paper("2609.00001", title="Do reasoning traces omit computation?", abstract="We test.")
KEYWORD = paper("2609.00002", title="Faithful summarisation", abstract="News summaries.")
INTERP_ONLY = paper("2609.00003", title="Sparse autoencoders at scale", abstract="Features.")
OFF = paper("2609.00004", title="Protein folding", abstract="Structures.")
PAPERS = [ON_TOPIC, KEYWORD, INTERP_ONLY, OFF]


class Models:
    """Scripted labs. Triage says yes by title; graders read `grades[(role, paper)]`."""

    def __init__(self):
        self.grades: dict[tuple[str, str], str] = {}
        self.down: set[str] = set()
        self.unrelated: set[str] = set()  # papers triage turns away
        self.calls: list[tuple[str, str]] = []

    def respond(self, role, request):
        title = request.user.splitlines()[0].removeprefix("Title: ")
        paper_id = next(p.id for p in PAPERS if p.title == title)
        who = "triage" if "sort arXiv papers" in request.system else role.label
        if who != "triage":
            who = "judge" if role.provider == "anthropic" else "check"
        self.calls.append((who, paper_id))
        if who in self.down or paper_id in self.down:
            raise ProviderError("InternalServerError", transient=False)
        if who == "triage":
            return json.dumps(
                {
                    "cot_faithfulness": {
                        "why": "w",
                        "relation": "related"
                        if paper_id == ON_TOPIC.id and paper_id not in self.unrelated
                        else "unrelated",
                    },
                    "interpretability": {"yes": paper_id in (ON_TOPIC.id, INTERP_ONLY.id)},
                    "agents": {"yes": False},
                }
            )
        grade = self.grades.get((who, paper_id), "none")
        return json.dumps({"reason": f"{who} says {grade}", "grade": grade})

    def gateway(self, config, journal):
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=())


@pytest.fixture
async def models(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, PAPERS)
    return Models()


async def table(conn, name, order="1, 2"):
    return await (await conn.execute(f"SELECT * FROM {name} ORDER BY {order}")).fetchall()


def test_the_triage_prompt_asks_about_every_topic_and_separates_the_two_kinds():
    request = screening.triage_request(ON_TOPIC, TOPICS)
    assert request.system.index("# Watch topics") < request.system.index("# Field topics")
    assert "`cot_faithfulness`" in request.system and FAITH.rubric not in request.system
    schema = request.output.model_json_schema()
    assert schema["required"] == ["cot_faithfulness", "interpretability", "agents"]
    assert "Title: Do reasoning traces" in request.user


def test_the_grading_prompt_carries_the_rubric_and_explains_tiers_only_when_it_uses_them():
    plain = watch.grading_request(ON_TOPIC, FAITH).system
    assert FAITH.rubric in plain and "tier 1 is must_read" not in plain
    tiered = FAITH.model_copy(update={"rubric": "Benchmarks; single-domain audits are tier 2."})
    assert "tier 1 is must_read" in watch.grading_request(ON_TOPIC, tiered).system


async def test_a_nightly_run_triages_audits_and_grades(conn, models):
    models.grades = {
        ("judge", ON_TOPIC.id): "must_read",
        ("check", ON_TOPIC.id): "relevant",
        ("judge", KEYWORD.id): "none",
        ("check", KEYWORD.id): "none",
    }
    run_id = await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)

    assert {r["paper_id"] for r in await table(conn, "triage")} == {p.id for p in PAPERS}
    members = [(r["paper_id"], r["topic"]) for r in await table(conn, "memberships")]
    assert members == [(ON_TOPIC.id, "interpretability"), (INTERP_ONLY.id, "interpretability")]
    # Triage found one; the keyword audit found the other without any model agreeing.
    found = {
        r["paper_id"]: (r["by_triage"], r["by_keyword"], r["state"], r["reason"])
        for r in await table(conn, "candidates")
    }
    assert found == {
        ON_TOPIC.id: (True, False, "graded", "w"),
        KEYWORD.id: (False, True, "graded", "keyword: faithful"),
    }
    # Disagreement keeps the more inclusive grade and is marked for a person to settle.
    split = await verdicts.current(conn, ON_TOPIC.id, FAITH.slug)
    assert (split.grade, split.split) == (Grade.MUST_READ, True)
    assert split.judge.reason == "judge says must_read" and split.check.grade == Grade.RELEVANT
    assert split.judge.model == "anthropic:claude-opus-5-5@medium"
    agreed = await verdicts.current(conn, KEYWORD.id, FAITH.slug)
    assert (agreed.grade, agreed.split) == (Grade.NONE, False)

    (run,) = await table(conn, "runs")
    assert run["id"] == run_id and run["error"] is None and run["finished_at"] is not None
    assert run["counts"] | {"calls": None} == {
        "triaged": 4,
        "candidates": 2,
        "candidates_keyword_only": 1,
        "graded": 2,
        "grade_must_read": 1,
        "grade_none": 1,
        "split": 1,
        "calls": None,
    }
    assert run["counts"]["calls"]["triage"]["calls"] == 4
    recorded = [e["type"] for e in await table(conn, "outbox", "id")]
    assert recorded.count("verdict.recorded") == 2

    # A second run finds nothing to do and pays for nothing.
    before = len(models.calls)
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    assert len(models.calls) == before


async def test_a_candidate_without_two_opinions_gets_no_verdict_and_is_retried(conn, models):
    models.grades = {("judge", ON_TOPIC.id): "relevant", ("check", ON_TOPIC.id): "relevant"}
    models.down = {"check"}
    config = Config()
    for attempt in (1, 2):
        await nightly.run(conn, config, fetch=False, gateway=models.gateway)
        (candidate,) = await triage_store.candidates_for(conn, ON_TOPIC.id)
        assert (candidate.state, candidate.failed_runs) == ("pending", attempt)
        assert candidate.last_error == "check: provider: InternalServerError"
    assert await verdicts.current(conn, ON_TOPIC.id, FAITH.slug) is None
    # The judge's validated answer is reused; only the missing opinion is paid for again.
    assert models.calls.count(("judge", ON_TOPIC.id)) == 1
    await nightly.run(conn, config, fetch=False, gateway=models.gateway)
    # After three failed runs it is left for the owner to see, not retried for ever.
    stuck = await triage_store.ungraded(conn, 3)
    assert {c.paper_id for c in stuck} == {ON_TOPIC.id, KEYWORD.id}
    models.down = set()
    await nightly.run(conn, config, fetch=False, gateway=models.gateway)
    assert await verdicts.current(conn, ON_TOPIC.id, FAITH.slug) is None


async def test_a_failed_triage_call_is_retried_and_a_keyword_match_still_makes_a_candidate(
    conn, models
):
    models.down = {KEYWORD.id, OFF.id}
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    states = {r["paper_id"]: r["state"] for r in await table(conn, "triage")}
    assert states[KEYWORD.id] == states[OFF.id] == "failed" and states[ON_TOPIC.id] == "done"
    (candidate,) = await triage_store.candidates_for(conn, KEYWORD.id)
    assert candidate.by_keyword and not candidate.by_triage
    models.down = set()
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    assert {r["state"] for r in await table(conn, "triage")} == {"done"}


async def test_a_rewritten_paper_is_triaged_again_and_its_old_verdict_is_kept_as_history(
    conn, models
):
    await papers_store.upsert(conn, [ON_TOPIC.model_copy(update={"version": 1})])
    models.grades = {("judge", ON_TOPIC.id): "relevant", ("check", ON_TOPIC.id): "relevant"}
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    # A touched-up revision keeps what it has.
    await papers_store.upsert(conn, [ON_TOPIC.model_copy(update={"version": 2})])
    assert await triage_store.untriaged(conn) == []
    rewrite = ON_TOPIC.model_copy(
        update={"version": 3, "abstract": "An entirely different abstract about other things."}
    )
    await papers_store.upsert(conn, [rewrite])
    assert [p.id for p in await triage_store.untriaged(conn)] == [ON_TOPIC.id]
    models.grades = {("judge", ON_TOPIC.id): "none", ("check", ON_TOPIC.id): "none"}
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    history = await verdicts.history(conn, ON_TOPIC.id, FAITH.slug)
    assert [(v.paper_version, v.grade) for v in history] == [(1, Grade.RELEVANT), (3, Grade.NONE)]
    assert (await verdicts.current(conn, ON_TOPIC.id, FAITH.slug)).grade == Grade.NONE


async def test_a_rewrite_is_graded_again_when_arxiv_gives_no_version_number(conn, models):
    """The version is often unknown, before and after, so it cannot be what marks a rewrite."""
    await papers_store.upsert(conn, [ON_TOPIC.model_copy(update={"version": 1})])
    models.grades = {("judge", ON_TOPIC.id): "relevant", ("check", ON_TOPIC.id): "relevant"}
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    for n, grade in ((2, "none"), (3, "adjacent")):
        rewrite = ON_TOPIC.model_copy(
            update={"updated": f"2026-10-0{n}T00:00:00+00:00", "abstract": f"{n} " * 40}
        )
        await papers_store.upsert(conn, [rewrite])
        models.grades = {("judge", ON_TOPIC.id): grade, ("check", ON_TOPIC.id): grade}
        await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
        current = await verdicts.current(conn, ON_TOPIC.id, FAITH.slug)
        assert (current.paper_version, current.paper_revision, current.grade) == (None, n, grade)
    (candidate,) = await triage_store.candidates_for(conn, ON_TOPIC.id)
    assert (candidate.state, candidate.paper_revision) == ("graded", 3)


async def test_a_nightly_run_is_not_marked_finished_while_it_is_still_working(
    conn, models, monkeypatch
):
    seen = {}

    async def collect(conn, categories, since, run_id):
        return SimpleNamespace(fetched=4, inserted=4, updated=0, details={}, rewritten=[])

    async def triage(conn, config, run, **_):
        seen.update(await runs.get(conn, run.run_id))

    monkeypatch.setattr(nightly.intake, "collect", collect)
    monkeypatch.setattr(nightly.watch_jobs, "triage", triage)
    run_id = await nightly.run(conn, Config(), gateway=models.gateway)
    assert (seen["fetched"], seen["finished_at"]) == (4, None)
    assert (await runs.get(conn, run_id))["finished_at"] is not None


async def test_topic_edits_apply_forward_and_only_a_content_change_makes_a_revision(conn, models):
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    renamed = await topics_store.save(conn, FAITH.model_copy(update={"name": "Faithfulness"}))
    assert renamed.revision == 1
    edited = await topics_store.save(
        conn, FAITH.model_copy(update={"keywords": [*FAITH.keywords, "protein"]})
    )
    assert edited.revision == 2
    assert [
        r["revision"] for r in await table(conn, "topic_versions") if r["topic"] == FAITH.slug
    ] == [1, 2]
    # Nothing retroactive runs by itself: the paper the new keyword matches stays as it was.
    before = len(models.calls)
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    assert len(models.calls) == before
    assert await triage_store.candidates_for(conn, OFF.id) == []
    # A paused topic is asked about nothing.
    await topics_store.save(conn, edited.model_copy(update={"status": Status.PAUSED}))
    async with model_run(conn, Config(), "manual", gateway=models.gateway) as run:
        await watch_jobs.triage(conn, Config(), run, papers=[OFF])
    assert "cot-faithfulness" not in (await table(conn, "triage"))[-1]["answers"]


async def test_a_pending_candidate_under_an_older_topic_revision_is_dropped_from_the_queue(
    conn, models
):
    async with model_run(conn, Config(), "manual", gateway=models.gateway) as run:
        await watch_jobs.triage(conn, Config(), run)
    edited = await topics_store.save(conn, FAITH.model_copy(update={"rubric": "A new rubric."}))
    async with model_run(conn, Config(), "manual", gateway=models.gateway) as run:
        await watch_jobs.triage(conn, Config(), run, papers=[ON_TOPIC])
    pending = await triage_store.pending_candidates(conn, 3)
    queued = sorted((c.paper_id, c.topic_revision) for c, _ in pending)
    # The first candidate for the re-triaged paper is superseded; a late retry of it cannot
    # overtake the verdict of the newer one.
    assert queued == [(ON_TOPIC.id, edited.revision), (KEYWORD.id, 1)]
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    assert [v.topic_revision for v in await verdicts.history(conn, ON_TOPIC.id, FAITH.slug)] == [2]
    # A candidate put forward under the old wording and never graded is graded under the new.
    assert [v.topic_revision for v in await verdicts.history(conn, KEYWORD.id, FAITH.slug)] == [2]
    assert await triage_store.pending_candidates(conn, 3) == []


async def test_papers_triage_keeps_failing_on_are_retried_after_the_new_ones(conn, models):
    models.down = {OFF.id}
    for _ in range(3):
        await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    (run, *_) = await table(conn, "runs", "id DESC")
    assert run["counts"]["triage_stuck"] == 1
    new = paper("2609.00009", title="Newer", abstract="Later.")
    await papers_store.upsert(conn, [new])
    # Under a limit the stuck paper does not take the new paper's place, yet stays queued.
    assert [p.id for p in await triage_store.untriaged(conn, 1)] == [new.id]
    assert [p.id for p in await triage_store.untriaged(conn)] == [new.id, OFF.id]


async def test_a_rewrite_that_triage_turns_away_is_still_graded_if_the_old_text_had_a_verdict(
    conn, models
):
    """Otherwise the verdict on text that no longer exists would stay current for ever."""
    models.grades = {("judge", ON_TOPIC.id): "relevant", ("check", ON_TOPIC.id): "relevant"}
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    rewrite = ON_TOPIC.model_copy(update={"abstract": "Something else entirely. " * 10})
    await papers_store.upsert(conn, [rewrite])
    models.unrelated = {ON_TOPIC.id}
    models.grades = {("judge", ON_TOPIC.id): "none", ("check", ON_TOPIC.id): "none"}
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    (candidate,) = await triage_store.candidates_for(conn, ON_TOPIC.id)
    assert (candidate.by_triage, candidate.by_keyword) == (False, False)
    assert candidate.reason == "rewritten: was relevant"
    current = await verdicts.current(conn, ON_TOPIC.id, FAITH.slug)
    assert (current.paper_revision, current.grade) == (2, Grade.NONE)
    # A paper never graded above `none` gives triage nothing to overrule.
    await papers_store.upsert(conn, [OFF.model_copy(update={"abstract": "Other. " * 30})])
    await nightly.run(conn, Config(), fetch=False, gateway=models.gateway)
    assert await triage_store.candidates_for(conn, OFF.id) == []
