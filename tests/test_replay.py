"""Replay (feedback.md, evaluation): the four recall numbers per cohort, what a replay records,
and that it writes nothing outside the evaluation tables. Models are scripted, as in
test_steps."""

from pathlib import Path

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Classification, Config
from litwatch.core import Fit
from litwatch.core.classification import Source
from litwatch.core.evaluation import Outcome, recall
from litwatch.feedback import misses as misses_feedback
from litwatch.llm import ProviderError
from litwatch.quality import replay
from litwatch.store import classifications as classifications_store
from litwatch.store import evaluation, quality
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import FAITH, TREE, paper
from tests.test_steps import CONFIG, Scripted, decision, rows

LABELS = Path(__file__).parent.parent / "evals" / "labels"
TOPIC = FAITH.slug


def outcome(**fields) -> Outcome:
    return Outcome(
        **{"paper_id": "1", "topic": TOPIC, "cohort": "owner", "positive": True} | fields
    )


# -- the four numbers ---------------------------------------------------------------------------


def test_each_recall_counts_its_own_stage():
    (line,) = recall(
        [
            # The classifier alone, graded central: through every stage.
            outcome(paper_id="1", classifier=True, uncertain=False, fit=Fit.CENTRAL),
            # The band alone: a member, but not the classifier's.
            outcome(paper_id="2", classifier=False, uncertain=True, fit=Fit.RELATED),
            # The keyword check alone, graded adjacent: a member that grading let go.
            outcome(paper_id="3", classifier=False, uncertain=False, keyword="CoT",
                    fit=Fit.ADJACENT),
            # No way in, though grading (which runs whatever classification said) holds it.
            outcome(paper_id="4", classifier=False, uncertain=False, fit=Fit.CENTRAL),
        ]
    )  # fmt: skip
    assert (line.classification.hits, line.membership.hits) == (1, 3)
    assert (line.grading.hits, line.end_to_end.hits) == (3, 2)
    assert {s.n for s in (line.classification, line.membership, line.grading)} == {4}
    assert line.positives == 4 and line.end_to_end.value == 0.5


def test_failures_count_as_misses_and_a_stage_not_run_counts_nowhere():
    (line,) = recall(
        [
            # Classification failed: a member by keyword alone, never the classifier's.
            outcome(paper_id="1", error="timeout", keyword="CoT", fit=Fit.CENTRAL),
            # Grading failed: a miss for grading and end to end.
            outcome(paper_id="2", classifier=True, uncertain=False, grade_error="invalid"),
            # Not run yet (a run cut short): in no denominator.
            outcome(paper_id="3"),
        ]
    )
    assert (line.classification.hits, line.classification.n) == (1, 2)
    assert (line.membership.hits, line.membership.n) == (2, 2)
    assert (line.grading.hits, line.grading.n) == (1, 2)
    assert (line.end_to_end.hits, line.end_to_end.n) == (1, 2)
    assert line.grading_failed == 1 and line.positives == 3


def test_cohorts_are_never_pooled_and_negatives_are_counted_apart():
    through = {"classifier": True, "uncertain": False, "fit": Fit.CENTRAL}
    lines = recall(
        [
            outcome(paper_id="1", cohort="owner", **through),
            outcome(paper_id="1", cohort="legacy", **through),
            outcome(paper_id="2", cohort="legacy", classifier=False, uncertain=False, fit=Fit.NONE),
            outcome(paper_id="3", cohort="legacy", positive=False, **through),
        ]
    )
    owner, legacy = lines
    assert (owner.cohort, owner.positives, owner.end_to_end.value) == ("owner", 1, 1.0)
    assert (legacy.cohort, legacy.positives, legacy.end_to_end.value) == ("legacy", 2, 0.5)
    assert (legacy.negatives.hits, legacy.negatives.n) == (1, 1)


# -- the labels ---------------------------------------------------------------------------------


def test_the_label_files_load_into_their_cohorts_with_their_own_positives():
    labels = replay.load(LABELS)
    by_cohort: dict[str, tuple[int, int]] = {}
    for x in labels:
        n, pos = by_cohort.get(x.cohort, (0, 0))
        by_cohort[x.cohort] = (n + 1, pos + x.positive)
    assert by_cohort == {
        "owner": (212, 7),  # central or related
        "provisional": (90, 90),  # every one a positive
        "legacy": (953, 91),  # 0.14 tier 1–2
        "silver": (200, 42),
    }


def test_a_sample_draws_negatives_with_a_seed_and_keeps_every_positive():
    labels = replay.load(LABELS)
    topics = ["cot-faithfulness", "cot-monitoring"]
    drawn = replay.select(labels, topics, cohorts=["legacy"], negatives={"legacy": 50})
    assert {x.cohort for x in drawn} == {"legacy"}
    assert (sum(x.positive for x in drawn), sum(not x.positive for x in drawn)) == (91, 50)
    assert drawn == replay.select(labels, topics, cohorts=["legacy"], negatives={"legacy": 50})
    # A topic that is not a Focus topic of the tree is not replayed.
    assert replay.select(labels, ["cot-monitoring"]) == [
        x for x in labels if x.topic == "cot-monitoring"
    ]


# -- a replay on the database -------------------------------------------------------------------

# Three papers labelled on the Focus topic; B's title matches its keyword check.
A = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes on activations.")
B = paper("2610.00002", title="Is the chain of thought faithful?", abstract="We test traces.")
C = paper("2610.00003", title="Robot arms", abstract="Grasping.")
LABELLED = [
    replay.Labelled(A.id, TOPIC, "owner", True),
    replay.Labelled(B.id, TOPIC, "owner", True),
    replay.Labelled(C.id, TOPIC, "owner", False),
    replay.Labelled(A.id, TOPIC, "legacy", False),
]


@pytest.fixture
async def models(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [A, B, C])
    return Scripted()


async def start(conn, models, labels=LABELLED, config=CONFIG) -> replay.Replay:
    r = await replay.prepare(conn, labels)
    await replay.start(conn, config, r, {})
    await replay.run(conn, config, r, gateway=models.gateway)
    return r


async def contents(conn) -> dict[str, str]:
    """A digest of every table's rows, so a change of any row shows, not only of their count."""
    tables = await rows(
        conn,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'",
    )
    found = {}
    for t in tables:
        name = t["table_name"]
        query = (
            "SELECT md5(coalesce(string_agg(x::text, ',' ORDER BY x::text), '')) AS h "
            f'FROM "{name}" x'
        )
        found[name] = (await rows(conn, query))[0]["h"]
    return found


async def test_a_replay_writes_only_to_the_evaluation_tables(conn, models):
    # A miss: the owner's word, a `miss` membership and a Save, all before the replay.
    await misses_feedback.report(conn, C.id, TOPIC, Fit.CENTRAL)
    before = await contents(conn)
    r = await start(conn, models, [*LABELLED, *await replay.misses(conn)])
    after = await contents(conn)
    changed = {t for t in before if before[t] != after[t]}
    # The run record and its call journal carry the cost; nothing of the pipeline moves.
    assert changed == {"eval_runs", "eval_labels", "eval_results", "runs", "inference_calls"}
    run = await evaluation.get(conn, r.eval_run_id)
    assert run["finished_at"] is not None and float(run["cost"]) > 0


async def test_the_owners_memberships_never_count_and_grading_runs_whatever_classification_said(
    conn, models
):
    # C is reported missed (a `miss` membership) and labelled a member by the owner; the
    # classifier puts it nowhere near the topic.
    await misses_feedback.report(conn, C.id, TOPIC, Fit.CENTRAL)
    await classifications_store.put_owner(conn, C, TOPIC, True, TREE.get(TOPIC).version)
    assert {m.source for m in await classifications_store.memberships(conn, C.id)} == {
        Source.MISS,
        Source.OWNER,
    }
    models.decisions[A.title] = decision("cot-faithfulness")  # the classifier's member
    models.decisions[C.title] = decision("other")
    models.fits[("judge", C.title)] = {"reason": "Its subject.", "fit": "central"}
    r = await start(conn, models, [*LABELLED, *await replay.misses(conn)])
    assert ("judge", C.title) in models.asked and ("check", C.title) in models.asked
    found = {(o.cohort, o.paper_id): o for o in await evaluation.outcomes(conn, r.eval_run_id)}
    miss = found[("miss", C.id)]
    assert (miss.classifier, miss.uncertain, miss.keyword, miss.member) == (
        False,
        False,
        None,
        False,
    )
    assert miss.fit == Fit.CENTRAL  # grading alone would have kept it
    assert found[("owner", A.id)].classifier is True
    assert (found[("owner", B.id)].classifier, found[("owner", B.id)].keyword) == (
        False,
        "chain.of.thought",
    )
    by_cohort = {x.cohort: x for x in recall(found.values())}
    assert (by_cohort["miss"].membership.hits, by_cohort["miss"].grading.hits) == (0, 1)
    assert by_cohort["miss"].end_to_end.hits == 0
    # The miss is a cohort of its own; the owner's two positives are A and B.
    assert (by_cohort["owner"].positives, by_cohort["owner"].membership.hits) == (2, 2)


async def test_a_failed_grading_is_a_miss_and_a_resume_redoes_only_what_failed(conn, models):
    models.decisions[A.title] = decision("cot-faithfulness")
    models.fits[("judge", A.title)] = {"fit": "not a fit"}  # invalid: unusable
    models.fits[("check", B.title)] = ProviderError("APITimeoutError", timeout=True)
    r = await start(conn, models)
    (line, _) = recall(await evaluation.outcomes(conn, r.eval_run_id))
    assert (line.grading.hits, line.grading.n, line.grading_failed) == (0, 2, 2)
    stored = {x["paper_id"]: x for x in await evaluation.results(conn, r.eval_run_id)}
    assert stored[A.id]["fit"] is None and "invalid" in stored[A.id]["grade_error"]
    assert stored[C.id]["fit"] == "related" and stored[C.id]["judge"]["fit"] == "related"

    del models.fits[("judge", A.title)], models.fits[("check", B.title)]
    models.asked.clear()
    again = await replay.resume(conn, CONFIG, r.eval_run_id)
    assert again.to_classify == [] and set(again.to_grade) == {(A.id, TOPIC), (B.id, TOPIC)}
    await replay.run(conn, CONFIG, again, gateway=models.gateway)
    assert {step for step, _ in models.asked} == {"judge", "check"}
    assert {title for _, title in models.asked} == {A.title, B.title}
    (line, _) = recall(await evaluation.outcomes(conn, r.eval_run_id))
    assert (line.grading.hits, line.end_to_end.hits, line.grading_failed) == (2, 2, 0)
    run = await evaluation.get(conn, r.eval_run_id)
    assert len(run["run_ids"]) == 2  # its cost is both runs'


async def test_a_replay_resumes_only_under_the_same_configuration(conn, models):
    r = await start(conn, models)
    stricter = CONFIG.model_copy(update={"classification": Classification(focus_threshold=0.6)})
    with pytest.raises(ValueError, match="another configuration"):
        await replay.resume(conn, stricter, r.eval_run_id)
    assert (await replay.resume(conn, CONFIG, r.eval_run_id)).to_grade == []


async def test_the_estimate_goes_by_the_journal_and_counts_what_is_left(conn, models):
    r = await replay.prepare(conn, LABELLED)
    assert (len(r.to_classify), len(r.to_grade)) == (3, 3)
    first = await replay.estimate(conn, CONFIG, r)
    assert first == pytest.approx(
        {"classification": 3 * 0.0006, "grading": 3 * (0.003 + 0.0006)}
    )  # no calls yet to go by
    await start(conn, models)
    again = await replay.estimate(conn, CONFIG, r)  # every scripted call cost $0.001
    assert again == pytest.approx({"classification": 3 * 0.001, "grading": 3 * 0.002})


async def test_quality_shows_recall_from_the_latest_replay_and_the_report_names_the_losses(
    conn, models
):
    assert await quality.replay_recall(conn) is None
    models.decisions[A.title] = decision("cot-faithfulness")
    models.fits[("judge", B.title)] = {"reason": "Off.", "fit": "none"}
    models.fits[("check", B.title)] = {"reason": "Off.", "fit": "adjacent"}
    r = await start(conn, models)
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, "t", Config())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"authorization": "Bearer t"},
    ) as client:
        page = (await client.get("/api/settings/quality")).json()
    shown = page["recall"]
    assert shown["eval_run"] == r.eval_run_id and shown["cost"] > 0
    owner = next(x for x in shown["recall"] if x["cohort"] == "owner")
    assert (owner["topic"], owner["positives"]) == (TOPIC, 2)
    assert owner["end_to_end"] == {"hits": 1, "n": 2}

    run = await evaluation.get(conn, r.eval_run_id)
    outcomes = await evaluation.outcomes(conn, r.eval_run_id)
    text = replay.report(
        run, recall(outcomes), outcomes, await evaluation.results(conn, r.eval_run_id)
    )
    assert "| owner | cot-faithfulness | 2 | 0.50 (1/2) | 1.00 (2/2) | 0.50 (1/2) |" in text
    assert "a member; graded adjacent (judge none, check adjacent)" in text
