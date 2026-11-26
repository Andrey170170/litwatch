"""Grading as a nightly step (pipeline.md, section 4): the graders each depth asks, what a
verdict records with its event, failures, and grading a stuck paper now. Models are scripted
through the real Gateway, as in test_steps."""

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import pytest

from litwatch import grading, intake
from litwatch.api import create_app
from litwatch.archive import ArchiveClient, ArchiveError
from litwatch.archive.client import listed
from litwatch.core import FAILED_RUNS, Cause, Fit, Status
from litwatch.core.grading import FIT_DEFINITIONS
from litwatch.feedback import word
from litwatch.gates.database import LOCKS, WriteUnit
from litwatch.intake import atom, oai
from litwatch.jobs import intake as intake_job
from litwatch.jobs import nightly
from litwatch.jobs.context import model_run
from litwatch.llm import ProviderError
from litwatch.store import classifications as classifications_store
from litwatch.store import failures, runs
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts as verdicts_store
from tests import sessions, settling
from tests.conftest import FAITH, INTERP, TREE, AppClient, started
from tests.fakes import scripted_gate, unavailable
from tests.fakes_http import FakeArchive, mock
from tests.scripted import (
    ANALYSIS,
    CONFIG,
    LISTED,
    A,
    B,
    Scripted,
    calls_for,
    decision,
    night,
    refusal,
    rows,
)
from tests.sessions import Bridge, Session, in_read, in_write

TOKEN = "s3cret"


@pytest.fixture
async def models(conn: Session):
    await settling.save_tree(conn, TREE)
    await in_write(conn, papers_store.upsert, [A, B])
    return Scripted()


def fit(value: str, reason: str = "Because.") -> dict:
    return {"reason": reason, "fit": value}


async def verdicts(conn: Session, paper_id: str) -> list[dict]:
    return await rows(conn, "SELECT * FROM verdicts WHERE paper_id = %s ORDER BY id", paper_id)


def test_every_grader_is_given_the_fit_definitions_word_for_word():
    for meaning in FIT_DEFINITIONS.values():
        assert meaning in grading.SYSTEM and meaning in grading.AREA_CHECK


async def test_a_focus_topic_is_graded_by_both_labs_and_the_more_inclusive_fit_stands(
    conn: Session, models
):
    # B matches CoT faithfulness's keyword: graded for that Focus topic alone.
    models.fits[("judge", B.title)] = fit("central")
    models.fits[("check", B.title)] = fit("adjacent")
    counts = await night(conn, models)
    assert counts["graded"] == 1
    (v,) = await verdicts(conn, B.id)
    assert (v["topic"], v["fit"], v["split"], v["escalated"]) == (
        FAITH.slug,
        "central",
        True,
        False,
    )
    assert (v["topic_version"], v["paper_revision"]) == (FAITH.version, 1)
    assert {r: o["fit"] for r, o in v["opinions"].items()} == {
        "judge": "central",
        "check": "adjacent",
    }
    assert v["opinions"]["check"]["model"] == "openrouter:test/check"
    (moved,) = await rows(conn, "SELECT payload FROM outbox WHERE type = 'grade.changed'")
    assert (moved["payload"]["after"], moved["payload"]["cause"]) == ("must_read", "verdict")
    (event,) = await rows(conn, "SELECT payload FROM outbox WHERE type = 'verdict.recorded'")
    assert event["payload"] == {
        "topic": FAITH.slug,
        "revision": 1,
        "verdict_id": v["id"],
        "fit": "central",
        "weight": "solid",
        "grade": "must_read",
        "split": True,
        "escalated": False,
        "one_lab": False,
    }
    # The verdict settles the paper in the same run: its effective grade, rarity and library.
    (g,) = await rows(conn, "SELECT * FROM effective_grades WHERE paper_id = %s", B.id)
    assert (g["fit"], g["grade"], g["rarity"]) == ("central", "must_read", "epic")
    (saved,) = await rows(conn, "SELECT saved FROM library WHERE paper_id = %s", B.id)
    assert saved["saved"] is True
    # Subject for every topic, the rubric for a Focus topic.
    asked = next(u for u in models.users if u.startswith("Topic: ") and B.title in u)
    assert FAITH.subject in asked and FAITH.rubric in asked
    await night(conn, models)
    assert len(await verdicts(conn, B.id)) == 1  # graded once per revision


@pytest.mark.parametrize(
    ("holds", "escalated"),
    [(0.9, False), (0.6, True), (0.3, True)],  # sure; a doubtful yes; a no
)
async def test_an_area_topic_escalates_to_check_on_doubt_and_the_less_inclusive_fit_stands(
    conn: Session, models, holds, escalated
):
    models.decisions[A.title] = decision("other", interpretability=0.9)
    models.fits[("judge", A.title)] = fit("central")
    models.fits[("area_check", A.title)] = {"holds": holds}
    models.fits[("check", A.title)] = fit("adjacent")
    await night(conn, models)
    (v,) = await verdicts(conn, A.id)
    assert (v["topic"], v["escalated"], v["split"]) == (INTERP.slug, escalated, escalated)
    assert v["fit"] == ("adjacent" if escalated else "central")
    assert v["opinions"]["area_check"]["probability"] == holds
    assert (("check", A.title) in models.asked) is escalated
    asked = next(u for u in models.users if u.startswith("Topic: ") and A.title in u)
    assert INTERP.subject in asked and "Rubric:" not in asked
    # An Area grade never saves.
    assert await rows(conn, "SELECT 1 FROM library WHERE saved AND paper_id = %s", A.id) == []


async def test_a_classified_paper_has_a_rarity_whatever_else_fails(conn: Session, models):
    models.analyses[A.title] = {"gist": "Too short."}  # invalid: no Weight
    await night(conn, models)  # A leads to no topic either
    (rarity,) = await rows(conn, "SELECT * FROM rarities WHERE paper_id = %s", A.id)
    assert (rarity["level"], rarity["best_topic"]) == ("uncommon", None)  # solid's floor


async def test_a_field_topic_is_graded_by_field_grade_alone(conn: Session, models):
    models.decisions[A.title] = decision("agents", agents=0.9)
    await night(conn, models)
    (v,) = await verdicts(conn, A.id)
    assert (v["topic"], list(v["opinions"]), v["split"]) == ("agents", ["field_grade"], False)
    assert ("judge", A.title) not in models.asked


async def test_a_rewritten_paper_is_graded_again_where_it_held_a_grade(conn: Session, models):
    models.decisions[A.title] = decision("other", interpretability=0.9)
    await night(conn, models)
    rewritten = A.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await in_write(conn, papers_store.upsert, [rewritten])
    models.decisions[A.title] = decision("other")  # no longer under Interpretability
    await night(conn, models)
    assert [(v["paper_revision"], v["topic"]) for v in await verdicts(conn, A.id)] == [
        (1, INTERP.slug),
        (2, INTERP.slug),
    ]


async def test_a_rewrite_leaves_a_topic_that_held_none(conn: Session, models):
    models.decisions[A.title] = decision("other", interpretability=0.9)
    models.fits[("judge", A.title)] = fit("none")
    await night(conn, models)
    await in_write(
        conn, papers_store.upsert, [A.model_copy(update={"abstract": "Something else."})]
    )
    models.decisions[A.title] = decision("other")
    await night(conn, models)
    assert [v["paper_revision"] for v in await verdicts(conn, A.id)] == [1]


async def test_a_topic_followed_later_grades_nothing_until_a_rerun_but_a_miss_does(
    conn: Session, models
):
    await night(conn, models)  # A leads to no topic; its pass is done all the same
    assert await verdicts(conn, A.id) == []
    models.decisions[A.title] = decision("other", interpretability=0.9)
    await in_write(
        conn,
        classifications_store.put_owner,
        A,
        "interpretability",
        True,
        TREE.get("interpretability").version,
    )  # now under a followed topic, as if a class had moved
    await night(conn, models)
    assert await verdicts(conn, A.id) == []
    await settling.report_miss(conn, A.id, INTERP.slug, Fit.RELATED)
    await night(conn, models)
    assert [v["topic"] for v in await verdicts(conn, A.id)] == [INTERP.slug]
    assert await in_read(conn, verdicts_store.candidates, FAILED_RUNS) == []  # the miss is done


async def test_the_limit_caps_papers_not_gradings(conn: Session, models):
    for p in (A, B):
        models.decisions[p.title] = decision("other", interpretability=0.9)
        models.fits[("judge", p.title)] = ProviderError("APITimeoutError", timeout=True)
    await night(conn, models)  # classified; every grading left to try again
    assert len(await in_read(conn, verdicts_store.candidates, FAILED_RUNS, limit=1)) == 1
    tree = await in_read(conn, taxonomy_store.load)
    due, _ = await in_read(conn, grading.due, tree, FAILED_RUNS, limit=1)
    assert [(d.paper.id, d.topic.slug) for d in due] == [(A.id, INTERP.slug)]
    due, _ = await in_read(conn, grading.due, tree, FAILED_RUNS)
    assert {(d.paper.id, d.topic.slug) for d in due} == {
        (A.id, INTERP.slug),
        (B.id, INTERP.slug),
        (B.id, FAITH.slug),
    }


@pytest.mark.parametrize(
    "error",
    [ProviderError("APITimeoutError", timeout=True), ProviderError("AuthenticationError: no key")],
)
async def test_a_grader_that_did_not_answer_is_retried_without_counting(
    conn: Session, models, error
):
    models.fits[("check", B.title)] = error
    for _ in range(FAILED_RUNS + 1):
        models.asked.clear()
        assert (await night(conn, models))["grading_failed"] == 1
        assert ("check", B.title) in models.asked
    (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE step = 'grading'")
    assert (failure["topic"], failure["failed_runs"]) == (FAITH.slug, 0)
    assert await verdicts(conn, B.id) == []


@asynccontextmanager
async def api(conn: Session, models, config=CONFIG):
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, config, models=models.gate(config))
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with AppClient(app, base_url="http://t", headers=headers) as c:
        yield c


async def stuck(conn: Session, models) -> None:
    """B's grading for its Focus topic failed three runs in a row: an unusable judge."""
    models.fits[("judge", B.title)] = fit("very central")
    for n in range(1, FAILED_RUNS + 1):
        assert (await night(conn, models))["grading_failed"] == 1
        (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE step = 'grading'")
        assert failure["failed_runs"] == n and failure["last_error"].startswith("invalid")
    del models.fits[("judge", B.title)]


async def test_an_unusable_answer_is_no_verdict_and_three_leave_it_for_the_owner(
    conn: Session, models
):
    # One grader answering unusably is enough: the other's answer alone is no verdict.
    await stuck(conn, models)
    assert await verdicts(conn, B.id) == []
    assert await in_read(conn, verdicts_store.candidates, FAILED_RUNS) == []
    models.asked.clear()
    await night(conn, models)
    assert ("judge", B.title) not in models.asked  # it waits for the owner now

    async with api(conn, models) as client:
        status = (await client.get("/api/summary")).json()["status"]
        assert status["state"] == "attention" and status["ungraded"] == 1
        quality = (await client.get("/api/settings/quality")).json()
        (listed,) = quality["night"]["ungraded"]
        assert (listed["paper_id"], listed["topic"], listed["revision"]) == (B.id, FAITH.slug, 1)

        url = "/api/settings/grading/{}/{}/now"
        now = url.format(FAITH.slug, B.id)
        assert (await client.post(now, json={"revision": 2})).status_code == 409
        elsewhere = url.format(INTERP.slug, B.id)
        assert (await client.post(elsewhere, json={"revision": 1})).status_code == 409
        unknown = url.format("nope", B.id)
        assert (await client.post(unknown, json={"revision": 1})).status_code == 404

        done = await client.post(now, json={"revision": 1})
        assert done.json() == {"graded": True, "fit": "related", "error": None}
        assert (await client.post(now, json={"revision": 1})).status_code == 409  # done
    (v,) = await verdicts(conn, B.id)
    assert v["fit"] == "related"
    assert await in_read(conn, failures.stuck, "grading", FAILED_RUNS) == []


NOW = f"/api/settings/grading/{FAITH.slug}/{B.id}/now"


async def test_grading_now_cut_short_is_left_to_the_next_night(conn: Session, models, monkeypatch):
    await stuck(conn, models)

    async def crash(unit: WriteUnit, g):
        raise RuntimeError("the request went away")

    monkeypatch.setattr(grading, "record", crash)
    async with api(conn, models) as client:
        with pytest.raises(RuntimeError):
            await client.post(NOW, json={"revision": 1})
    monkeypatch.undo()
    (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE step = 'grading'")
    assert failure["failed_runs"] == 0  # pending, with three runs to go
    assert (await night(conn, models))["graded"] == 1
    assert [v["fit"] for v in await verdicts(conn, B.id)] == ["related"]


async def test_grading_now_on_text_that_changed_meanwhile_is_refused(
    conn: Session, models, monkeypatch
):
    await stuck(conn, models)

    async def rewritten(unit: WriteUnit, paper):
        return False

    monkeypatch.setattr(papers_store, "still_current", rewritten)
    async with api(conn, models) as client:
        assert (await client.post(NOW, json={"revision": 1})).status_code == 409
    monkeypatch.undo()
    assert await verdicts(conn, B.id) == []
    (run,) = await rows(conn, "SELECT counts FROM runs WHERE kind = 'grading'")
    assert "graded" not in run["counts"] and "grading_failed" not in run["counts"]


def paused(slug: str = FAITH.slug):
    return TREE.model_copy(
        update={
            "topics": [
                t.model_copy(update={"status": Status.PAUSED}) if t.slug == slug else t
                for t in TREE.topics
            ]
        }
    )


async def test_a_paused_topic_is_not_graded_now_nor_waits_for_the_owner(conn: Session, models):
    await stuck(conn, models)
    await settling.save_taxonomy(conn, paused())
    async with api(conn, models) as client:
        assert (await client.post(NOW, json={"revision": 1})).status_code == 409
        status = (await client.get("/api/summary")).json()["status"]
        quality = (await client.get("/api/settings/quality")).json()
    assert status["ungraded"] == 0 and quality["night"]["ungraded"] == []
    (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE step = 'grading'")
    assert failure["failed_runs"] == FAILED_RUNS  # nothing reset, nothing spent
    assert await rows(conn, "SELECT 1 FROM runs WHERE kind = 'grading'") == []


async def test_a_failure_on_a_paused_topic_does_not_wait_for_the_owner(conn: Session, models):
    await stuck(conn, models)
    paper = await in_read(conn, papers_store.get, B.id)
    assert await in_read(conn, failures.waiting, paper, "grading", FAILED_RUNS, FAITH.slug)
    await settling.save_taxonomy(conn, paused())
    assert not await in_read(conn, failures.waiting, paper, "grading", FAILED_RUNS, FAITH.slug)


async def test_grading_now_checks_the_topic_under_the_taxonomy_lock(
    conn: Session, models, monkeypatch
):
    """No topic edit can land between grading now's look at the topic and what it does: when
    it locks the paper, a pause trying to take the taxonomy lock finds it held."""
    await stuck(conn, models)
    landed = []
    lock = papers_store.lock

    async def pause_meanwhile(c, paper_id):
        async with await sessions.sibling(conn) as other, other.transaction():
            row = await (
                await other.execute(
                    "SELECT pg_try_advisory_xact_lock(%s) AS got", (LOCKS["taxonomy"].key,)
                )
            ).fetchone()
            if row["got"]:
                await settling.save_taxonomy(other, paused())
            landed.append(row["got"])
        return await lock(c, paper_id)

    monkeypatch.setattr(papers_store, "lock", pause_meanwhile)
    async with api(conn, models) as client:
        await client.post(NOW, json={"revision": 1})
    assert landed == [False]


async def test_a_grading_whose_topic_was_paused_during_the_call_is_dropped(
    conn: Session, db, models
):
    paper = await in_read(conn, papers_store.get, B.id)
    due = grading.Due(paper, FAITH, "Chain-of-thought faithfulness")
    async with (
        model_run(db, CONFIG, "grading", models=models.gate(CONFIG)) as run,
        grading.grade([due], run.gateway) as results,
    ):
        (graded,) = [g async for g in results]
    assert graded.verdict is not None
    await settling.save_taxonomy(conn, paused())  # paused while the graders were asked
    async with settling.change(conn, B.id, cause=Cause.VERDICT) as change:
        assert await grading.record(change.unit, graded) is False
    assert await verdicts(conn, B.id) == []
    assert await rows(conn, "SELECT 1 FROM outbox WHERE type = 'verdict.recorded'") == []


async def test_a_miss_whose_grading_stopped_is_no_longer_a_candidate(conn: Session, models):
    await night(conn, models)  # A leads to no topic; its pass is done
    await settling.report_miss(conn, A.id, INTERP.slug, Fit.RELATED)
    models.fits[("judge", A.title)] = fit("very central")
    for _ in range(FAILED_RUNS):
        await night(conn, models)
    assert await in_read(conn, verdicts_store.candidates, FAILED_RUNS) == []


# B's second version as the archive lists it: a new abstract, so a rewrite.
B_REWRITTEN = {
    "id": B.id,
    "version": 2,
    "title": B.title,
    "abstract": "An entirely different abstract about robots.",
    "authors": ["Alice Smith"],
    "categories": ["cs.CL"],
    "primary_category": None,
    "published": "2026-10-02",
    "updated": "2026-10-10",
    "version_history": [],
    "doi": None,
    "journal_ref": None,
    "license": None,
}
WINDOW = (date(2026, 10, 1), date(2026, 10, 14))


async def assert_settled_on_solid(conn: Session) -> None:
    (g,) = await rows(conn, "SELECT * FROM effective_grades WHERE paper_id = %s", B.id)
    assert g["grade"] == "relevant"  # the new text has no Weight yet: solid
    (saved,) = await rows(conn, "SELECT saved FROM library WHERE paper_id = %s", B.id)
    assert saved["saved"] is True


async def test_a_rewrite_read_in_settles_the_paper_on_solid(conn: Session, db, models):
    models.analyses[B.title] = ANALYSIS | {"weight": "incremental"}
    await night(conn, models)  # related and incremental: adjacent, not saved
    fake = FakeArchive()
    fake.listing = [B_REWRITTEN]
    client = ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))
    result = await intake_job.from_archive(db, CONFIG, client, *WINDOW)
    assert result.rewritten == [B.id]
    await assert_settled_on_solid(conn)


class FailingLater:
    """An archive client whose listing fails after its first page."""

    async def papers(self, since, until, categories):
        yield [listed(B_REWRITTEN)]
        raise ArchiveError("the archive went away")


async def test_a_rewrite_settles_with_its_page_when_a_later_page_fails(conn: Session, db, models):
    models.analyses[B.title] = ANALYSIS | {"weight": "incremental"}
    await night(conn, models)
    with pytest.raises(ArchiveError):
        await intake_job.from_archive(db, CONFIG, FailingLater(), *WINDOW)
    assert (await in_read(conn, papers_store.get, B.id)).version == 2  # the first page is stored
    await assert_settled_on_solid(conn)


async def test_a_rewrite_whose_settle_fails_is_not_stored(conn: Session, db, models, monkeypatch):
    async def failing(unit, paper_ids, **kwargs):
        raise RuntimeError("settle failed")

    monkeypatch.setattr(word, "settle_many", failing)
    fake = FakeArchive()
    fake.listing = [B_REWRITTEN]
    client = ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))
    with pytest.raises(RuntimeError, match="settle failed"):
        await intake_job.from_archive(db, CONFIG, client, *WINDOW)
    stored = await in_read(conn, papers_store.get, B.id)
    assert (stored.version, stored.revision, stored.abstract) == (B.version, 1, B.abstract)
    assert await rows(conn, "SELECT * FROM outbox WHERE type = 'paper.revised'") == []


async def test_a_rewrite_harvested_from_arxiv_settles_as_it_is_stored(
    conn: Session, models, monkeypatch
):
    models.analyses[B.title] = ANALYSIS | {"weight": "incremental"}
    await night(conn, models)
    now = datetime.now(UTC) - timedelta(minutes=1)
    rewrite = B.model_copy(update={"abstract": B_REWRITTEN["abstract"], "updated": now.isoformat()})
    monkeypatch.setattr(atom, "fetch_category", unavailable)
    monkeypatch.setattr(oai, "fetch_category", lambda *a, **k: oai.OaiBatch([rewrite], now))
    run_id = await started(conn, "intake")
    result = await intake.collect(
        Bridge(conn), ("cs.CL",), now - timedelta(days=3), run_id, rules=settling.RULES
    )
    assert result.rewritten == [B.id]
    await assert_settled_on_solid(conn)


# -- the graders' lists (models.md, roles; pipeline.md, section 4) ---------------------------------

JUDGES = [m.model for m in LISTED.models.judge.models]
CHECKS = [m.model for m in LISTED.models.check.models]


def refused_by(models: Scripted, listed: list[str], title: str) -> None:
    for model in listed:
        models.said[(model, title)] = refusal(model)


async def test_a_focus_side_refused_throughout_leaves_the_others_fit_standing(
    conn: Session, models
):
    refused_by(models, JUDGES, B.title)
    models.fits[("check", B.title)] = fit("central")
    assert (await night(conn, models, LISTED))["graded"] == 1
    assert await calls_for(conn, B.id, "judge") == JUDGES  # down the list in one run
    (v,) = await verdicts(conn, B.id)
    assert (v["fit"], v["one_lab"], v["split"]) == ("central", True, False)
    assert list(v["opinions"]) == ["check"]
    (event,) = await rows(conn, "SELECT payload FROM outbox WHERE type = 'verdict.recorded'")
    assert event["payload"]["one_lab"] is True
    assert await rows(conn, "SELECT 1 FROM step_failures") == []
    # Central from one lab waits on Needs you for the owner's word, as a split does.
    async with api(conn, models, LISTED) as client:
        quality = (await client.get("/api/settings/quality")).json()
        (listed,) = quality["night"]["splits"]
        assert (listed["paper_id"], listed["one_lab"], listed["fit"]) == (B.id, True, "central")
        page = (await client.get(f"/api/papers/{B.id}")).json()
        assert [(s["kind"], s["topic"]) for s in page["states"]] == [("one_lab", FAITH.slug)]


async def test_a_one_lab_grade_below_related_waits_for_no_one(conn: Session, models):
    refused_by(models, CHECKS, B.title)
    models.fits[("judge", B.title)] = fit("adjacent")
    await night(conn, models, LISTED)
    (v,) = await verdicts(conn, B.id)
    assert (v["fit"], v["one_lab"], list(v["opinions"])) == ("adjacent", True, ["judge"])
    async with api(conn, models, LISTED) as client:
        assert (await client.get("/api/settings/quality")).json()["night"]["splits"] == []


async def test_a_refused_side_is_not_asked_again_while_the_other_has_not_answered(
    conn: Session, models
):
    refused_by(models, JUDGES, B.title)
    models.fits[("check", B.title)] = ProviderError("APITimeoutError", timeout=True)
    assert (await night(conn, models, LISTED))["grading_failed"] == 1
    (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE step = 'grading'")
    assert (failure["failed_runs"], failure["refused"]) == (0, ["judge"])  # still pending
    assert await in_read(conn, failures.stuck, "grading", FAILED_RUNS) == []
    models.fits[("check", B.title)] = fit("related")
    assert (await night(conn, models, LISTED))["graded"] == 1
    assert await calls_for(conn, B.id, "judge") == JUDGES  # the first night's, and no more
    (v,) = await verdicts(conn, B.id)
    assert (v["fit"], v["one_lab"]) == ("related", True)


async def test_a_refused_side_is_asked_again_once_the_question_changed(conn: Session, models):
    refused_by(models, JUDGES, B.title)
    models.fits[("check", B.title)] = ProviderError("APITimeoutError", timeout=True)
    await night(conn, models, LISTED)
    # The owner rewords the topic before the next night; judge now answers it.
    for table, key, column in (("topics", "class", "subject"), ("classes", "slug", "description")):
        await conn.execute(
            f"UPDATE {table} SET {column} = {column} || ' Reworded.' WHERE {key} = %s",
            (FAITH.slug,),
        )
    del models.said[(JUDGES[0], B.title)]
    models.fits[("check", B.title)] = fit("related")
    assert (await night(conn, models, LISTED))["graded"] == 1
    assert await calls_for(conn, B.id, "judge") == [*JUDGES, JUDGES[0]]
    (v,) = await verdicts(conn, B.id)
    assert (v["one_lab"], sorted(v["opinions"])) == (False, ["check", "judge"])


async def test_a_remembered_refusal_with_its_provider_gone_waits_without_counting(
    conn: Session, db, models
):
    refused_by(models, JUDGES, B.title)
    models.fits[("check", B.title)] = ProviderError("APITimeoutError", timeout=True)
    await night(conn, models, LISTED)

    # judge's lab lost its credentials
    openrouter_only = scripted_gate(models.respond, LISTED, ("openrouter",))

    models.fits[("check", B.title)] = fit("related")
    run_id = await nightly.run(db, LISTED, fetch=False, models=openrouter_only)
    assert (await in_read(conn, runs.get, run_id))["counts"]["grading_failed"] == 1
    (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE step = 'grading'")
    assert failure["failed_runs"] == 0 and "no credentials" in failure["last_error"]
    assert await verdicts(conn, B.id) == []  # no grade from one lab on a side not asked


async def test_a_remembered_refusal_gives_way_to_an_answer_journalled_since(
    conn: Session, db, models
):
    refused_by(models, JUDGES, B.title)
    models.fits[("check", B.title)] = ProviderError("APITimeoutError", timeout=True)
    await night(conn, models, LISTED)
    # An evaluation asks judge the identical question, and Sonnet answers it this time.
    models.said[(JUDGES[1], B.title)] = fit("central")
    tree = await in_read(conn, taxonomy_store.load)
    paper = await in_read(conn, papers_store.get, B.id)
    asked = grading.request(paper, tree.topic(FAITH.slug), tree.get(FAITH.slug).name)
    async with model_run(db, LISTED, "eval", models=models.gate(LISTED)) as run:
        await run.gateway.ask("judge", asked, paper_id=B.id)
    models.fits[("check", B.title)] = fit("adjacent")
    assert (await night(conn, models, LISTED))["graded"] == 1
    (v,) = await verdicts(conn, B.id)
    assert (v["fit"], v["split"], v["one_lab"]) == ("central", True, False)  # reused, not skipped


async def test_both_sides_refused_throughout_is_ungraded_at_once(conn: Session, models):
    refused_by(models, JUDGES, B.title)
    refused_by(models, CHECKS, B.title)
    assert (await night(conn, models, LISTED))["grading_failed"] == 1
    (stuck,) = await in_read(conn, failures.stuck, "grading", FAILED_RUNS)  # after one night
    assert (stuck["failed_runs"], stuck["refused"]) == (FAILED_RUNS, ["check", "judge"])
    assert stuck["stopped_by_refusal"] is True
    assert await verdicts(conn, B.id) == []
    models.models_asked.clear()
    await night(conn, models, LISTED)
    assert not [m for m, t in models.models_asked if t == B.title]  # it waits for the owner
    # Grading it now starts each side from its role's own model.
    del models.said[(JUDGES[0], B.title)], models.said[(CHECKS[0], B.title)]
    async with api(conn, models, LISTED) as client:
        done = await client.post(NOW, json={"revision": 1})
    assert done.json() == {"graded": True, "fit": "related", "error": None}
    (v,) = await verdicts(conn, B.id)
    assert (v["one_lab"], v["opinions"]["judge"]["model"]) == (False, f"anthropic:{JUDGES[0]}")


async def test_a_side_refused_throughout_while_the_other_fails_three_runs_is_said_as_such(
    conn: Session, models
):
    refused_by(models, JUDGES, B.title)
    for model in CHECKS:
        models.said[(model, B.title)] = fit("very central")  # invalid
    for _ in range(FAILED_RUNS):
        await night(conn, models, LISTED)
    (stuck,) = await in_read(conn, failures.stuck, "grading", FAILED_RUNS)
    assert (stuck["failed_runs"], stuck["refused"]) == (FAILED_RUNS, ["judge"])
    assert stuck["stopped_by_refusal"] is False  # the runs stopped it, not the refusal
    assert await calls_for(conn, B.id, "check") == CHECKS  # one model down each run
    async with api(conn, models, LISTED) as client:
        page = (await client.get(f"/api/papers/{B.id}")).json()
    assert [(s["kind"], s["refused"]) for s in page["states"]] == [("ungraded", False)]


async def test_a_refusal_after_a_failed_run_stops_at_three_runs(conn: Session, models):
    models.decisions[A.title] = decision("agents", agents=0.9)
    fields = [m.model for m in LISTED.models.field_grade.models]
    models.said[(fields[0], A.title)] = fit("very central")  # invalid: one failed run
    await night(conn, models, LISTED)
    refused_by(models, fields, A.title)
    await night(conn, models, LISTED)
    # Started one model down, the run went on to the top: every model refused.
    assert await calls_for(conn, A.id, "field_grade") == [*fields[:1], *fields[1:], *fields[:1]]
    (stuck,) = await in_read(conn, failures.stuck, "grading", FAILED_RUNS)
    assert (stuck["topic"], stuck["failed_runs"], stuck["stopped_by_refusal"]) == (
        "agents",
        FAILED_RUNS,
        True,
    )


async def test_an_area_or_field_grading_refused_throughout_has_no_one_lab_rule(
    conn: Session, models
):
    models.decisions[A.title] = decision("agents", agents=0.9, interpretability=0.9)
    refused_by(models, JUDGES, A.title)
    refused_by(models, [m.model for m in LISTED.models.field_grade.models], A.title)
    assert (await night(conn, models, LISTED))["grading_failed"] == 2
    stopped = await in_read(conn, failures.stuck, "grading", FAILED_RUNS)
    assert {(f["topic"], tuple(f["refused"])) for f in stopped} == {
        (INTERP.slug, ("judge",)),
        ("agents", ("field_grade",)),
    }
    assert ("area_check", A.title) not in models.asked and ("check", A.title) not in models.asked
    assert await verdicts(conn, A.id) == []


async def test_an_answer_that_came_in_is_kept_while_the_other_side_steps_down(
    conn: Session, models
):
    models.said[(CHECKS[0], B.title)] = fit("very central")  # invalid
    assert (await night(conn, models, LISTED))["grading_failed"] == 1
    assert (await night(conn, models, LISTED))["graded"] == 1
    # The judge's answer is reused, at no call; check's next run starts one model down (D4).
    assert await calls_for(conn, B.id, "judge") == JUDGES[:1]
    assert await calls_for(conn, B.id, "check") == CHECKS[:2]
    (v,) = await verdicts(conn, B.id)
    assert v["opinions"]["check"]["model"] == f"openrouter:{CHECKS[1]}"
