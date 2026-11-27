"""Replay (feedback.md, evaluation): the four recall numbers per cohort, what a replay records,
and that it writes nothing outside the evaluation tables. Models are scripted, as in
test_steps."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from litwatch import grading
from litwatch.api import create_app
from litwatch.config import Classification, Config
from litwatch.core import Depth, Fit, Status, Taxonomy, TaxonomyClass, Topic
from litwatch.core.budget import CallStart
from litwatch.core.classification import Source
from litwatch.core.evaluation import Outcome, recall
from litwatch.feedback import misses as misses_feedback
from litwatch.jobs import worker
from litwatch.llm import ModelGate, ProviderError, prices
from litwatch.quality import replay
from litwatch.store import classifications as classifications_store
from litwatch.store import evaluation, quality, runs
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store.calls import CallJournal
from tests import settling
from tests.conftest import FAITH, TREE, AppClient, gate_on, paper, started
from tests.fakes import BatchingProvider
from tests.priced import COST, Costed, Priced, charged
from tests.scripted import CONFIG, Scripted, decision, rows
from tests.sessions import Bridge, Session, in_read, in_write
from tests.working import QUIET, job_of, killed, served, step, until, work_out

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
        "owner": (217, 8),  # central or related
        "provisional": (86, 86),  # every one a positive
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
async def models(conn: Session):
    await settling.save_tree(conn, TREE)
    await in_write(conn, papers_store.upsert, [A, B, C])
    return Scripted()


async def test_the_cli_replay_that_loses_the_database_as_it_starts_submits_nothing(
    conn: Session, models, tmp_path, monkeypatch
):
    """`litwatch eval replay`: the database is lost as its start commits, in the unit that
    submits the replay's item, so both are rolled back and nothing runs."""
    import psycopg  # noqa: PLC0415

    from litwatch.store import work as queue  # noqa: PLC0415
    from tests.working import command  # noqa: PLC0415

    async def served(unit, kind, within):  # as if a `serve` ran the replay
        return True

    monkeypatch.setattr(queue, "served_here", served)
    monkeypatch.setattr(queue, "serving", served)  # its preflight's look

    labels = tmp_path / "labels"
    labels.mkdir()
    (labels / "fit.csv").write_text(
        f"paper_id,topic,cohort,fit\n{A.id},{TOPIC},owner,central\n{C.id},{TOPIC},owner,none\n"
    )
    (labels / "regression.csv").write_text("paper_id,topic,source,tier\n")
    real, ran = replay.start, []

    async def lost(unit, *args, **kwargs):
        await real(unit, *args, **kwargs)
        ran.append("started")
        raise psycopg.OperationalError("the server closed the connection unexpectedly")

    monkeypatch.setattr(replay, "start", lost)
    argv = ("eval", "replay", "--labels", str(labels), "--yes")
    result = await command(conn, tmp_path, monkeypatch, *argv)
    assert result.exit_code != 0, result.output
    assert ran == ["started"] and await rows(conn, "SELECT id FROM eval_runs") == []
    assert await rows(conn, "SELECT * FROM work_items") == []


async def start(conn: Session, models, labels=LABELLED, config=CONFIG) -> replay.Replay:
    r = await in_read(conn, replay.prepare, labels)
    r.eval_run_id = await in_write(conn, replay.start, config, r, {})
    async with gate_on(conn) as db:
        await replay.run(db, config, r, models=models.gate(config))
    return r


async def contents(conn: Session) -> dict[str, str]:
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


async def test_a_replay_writes_only_to_the_evaluation_tables(conn: Session, models):
    # A miss: the owner's word, a `miss` membership and a Save, all before the replay.
    async with Bridge(conn).write("report", taxonomy="shared", papers=[C.id]) as unit:
        await misses_feedback.report(unit, C.id, TOPIC, Fit.CENTRAL)
    before = await contents(conn)
    r = await start(conn, models, [*LABELLED, *await in_read(conn, replay.misses)])
    after = await contents(conn)
    changed = {t for t in before if before[t] != after[t]}
    # The run record and its call journal carry the cost; nothing of the pipeline moves, nor
    # what the status document reads of the providers.
    assert changed == {"eval_runs", "eval_labels", "eval_results", "runs", "inference_calls"}
    run = await in_read(conn, evaluation.get, r.eval_run_id)
    assert run["finished_at"] is not None and float(run["cost"]) > 0
    # An attempt killed before its run record was finished still counts what it spent.
    await conn.execute("UPDATE runs SET cost = 0")  # what a killed attempt leaves
    assert (await in_read(conn, evaluation.get, r.eval_run_id))["cost"] == run["cost"]


async def test_the_owners_memberships_never_count_and_grading_runs_whatever_classification_said(
    conn: Session, models
):
    # C is reported missed (a `miss` membership) and labelled a member by the owner; the
    # classifier puts it nowhere near the topic.
    async with Bridge(conn).write("report", taxonomy="shared", papers=[C.id]) as unit:
        await misses_feedback.report(unit, C.id, TOPIC, Fit.CENTRAL)
    await in_write(conn, classifications_store.put_owner, C, TOPIC, True, TREE.get(TOPIC).version)
    assert {m.source for m in await in_read(conn, classifications_store.memberships, C.id)} == {
        Source.MISS,
        Source.OWNER,
    }
    models.decisions[A.title] = decision("cot-faithfulness")  # the classifier's member
    models.decisions[C.title] = decision("other")
    models.fits[("judge", C.title)] = {"reason": "Its subject.", "fit": "central"}
    r = await start(conn, models, [*LABELLED, *await in_read(conn, replay.misses)])
    assert ("judge", C.title) in models.asked and ("check", C.title) in models.asked
    found = {
        (o.cohort, o.paper_id): o for o in await in_read(conn, evaluation.outcomes, r.eval_run_id)
    }
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


async def test_a_failed_grading_is_a_miss_and_a_resume_redoes_only_what_failed(
    conn: Session, db, models
):
    models.decisions[A.title] = decision("cot-faithfulness")
    models.fits[("judge", A.title)] = {"fit": "not a fit"}  # invalid: unusable
    models.fits[("check", B.title)] = ProviderError("APITimeoutError", timeout=True)
    r = await start(conn, models)
    (line, _) = recall(await in_read(conn, evaluation.outcomes, r.eval_run_id))
    assert (line.grading.hits, line.grading.n, line.grading_failed) == (0, 2, 2)
    stored = {x["paper_id"]: x for x in await in_read(conn, evaluation.results, r.eval_run_id)}
    assert stored[A.id]["fit"] is None and "invalid" in stored[A.id]["grade_error"]
    assert stored[C.id]["fit"] == "related" and stored[C.id]["judge"]["fit"] == "related"

    del models.fits[("judge", A.title)], models.fits[("check", B.title)]
    models.asked.clear()
    again = await in_read(conn, replay.resume, CONFIG, r.eval_run_id)
    assert again.to_classify == [] and set(again.to_grade) == {(A.id, TOPIC), (B.id, TOPIC)}
    await replay.run(db, CONFIG, again, models=models.gate(CONFIG))
    assert {step for step, _ in models.asked} == {"judge", "check"}
    assert {title for _, title in models.asked} == {A.title, B.title}
    (line, _) = recall(await in_read(conn, evaluation.outcomes, r.eval_run_id))
    assert (line.grading.hits, line.end_to_end.hits, line.grading_failed) == (2, 2, 0)
    run = await in_read(conn, evaluation.get, r.eval_run_id)
    assert len(run["run_ids"]) == 2  # its cost is both runs'


async def test_a_replay_resumes_only_under_the_same_configuration(conn: Session, models):
    r = await start(conn, models)
    stricter = CONFIG.model_copy(update={"classification": Classification(focus_threshold=0.6)})
    with pytest.raises(ValueError, match="another configuration"):
        await in_read(conn, replay.resume, stricter, r.eval_run_id)
    assert (await in_read(conn, replay.resume, CONFIG, r.eval_run_id)).to_grade == []


async def test_a_routing_change_refuses_a_resume(conn: Session, models):
    # An Area topic under the Focus topic, draft when the run starts: once active, a classifier
    # membership of its class would grade there instead, so finished and unfinished papers
    # would be routed differently.
    subject = "Probes that read hidden reasoning steps from a model's activations."
    probes = Topic(slug="cot-probes", depth=Depth.AREA, monogram="CP", colour="#123456",
                   subject=subject, status=Status.DRAFT)  # fmt: skip
    child = TaxonomyClass(slug="cot-probes", name="Probes of reasoning traces",
                          description=subject, parent=TOPIC)  # fmt: skip
    drafted = Taxonomy(classes=[*TREE.classes, child], topics=[*TREE.topics, probes])
    await settling.save_tree(conn, drafted)
    r = await start(conn, models)
    active = probes.model_copy(update={"status": Status.ACTIVE})
    await settling.save_tree(conn, Taxonomy(classes=drafted.classes, topics=[*TREE.topics, active]))
    with pytest.raises(ValueError, match="another configuration"):
        await in_read(conn, replay.resume, CONFIG, r.eval_run_id)


async def test_a_classifier_member_through_a_followed_class_below_is_the_classifiers(
    conn: Session, models
):
    """Membership is inherited: a paper the classifier puts in a class under the Focus topic is
    the topic's member, even when a followed Area there would grade it instead."""
    subject = "Probes that read hidden reasoning steps from a model's activations."
    probes = Topic(slug="cot-probes", depth=Depth.AREA, monogram="CP", colour="#123456",
                   subject=subject)  # fmt: skip
    child = TaxonomyClass(slug="cot-probes", name="Probes of reasoning traces",
                          description=subject, parent=TOPIC)  # fmt: skip
    await settling.save_tree(
        conn, Taxonomy(classes=[*TREE.classes, child], topics=[*TREE.topics, probes])
    )
    # The classifier puts A in the class below only (no probability on the Focus class itself).
    models.decisions[A.title] = decision(**{"cot-probes": 0.9}) | {
        "primary_class": {"option": child.name, "probability": 0.9}
    }
    r = await start(conn, models)
    found = {
        (o.cohort, o.paper_id): o for o in await in_read(conn, evaluation.outcomes, r.eval_run_id)
    }
    assert found[("owner", A.id)].classifier is True


async def test_a_prompt_change_refuses_a_resume(conn: Session, models, monkeypatch):
    r = await start(conn, models)
    monkeypatch.setattr(grading, "SYSTEM", grading.SYSTEM + " Answer briefly.")
    with pytest.raises(ValueError, match="another configuration"):
        await in_read(conn, replay.resume, CONFIG, r.eval_run_id)


async def test_a_change_to_the_default_routing_refuses_a_resume(conn: Session, models, monkeypatch):
    r = await start(conn, models)
    monkeypatch.setitem(replay.DEFAULT_ROUTING, CONFIG.models.check.model, ("another-provider",))
    with pytest.raises(ValueError, match="another configuration"):
        await in_read(conn, replay.resume, CONFIG, r.eval_run_id)


async def test_a_code_change_refuses_a_resume(conn: Session, models, monkeypatch):
    r = await start(conn, models)
    monkeypatch.setattr(replay, "_code", lambda: "another")
    with pytest.raises(ValueError, match="another configuration"):
        await in_read(conn, replay.resume, CONFIG, r.eval_run_id)


async def test_a_paper_whose_text_changed_is_classified_and_graded_again_on_resume(
    conn: Session, db, models
):
    # Classified on its first text, its grading failed; then its abstract was rewritten.
    models.decisions[A.title] = decision("cot-faithfulness")
    models.fits[("judge", A.title)] = {"fit": "not a fit"}
    r = await start(conn, models)
    rewritten = A.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await in_write(conn, papers_store.upsert, [rewritten])
    del models.fits[("judge", A.title)]
    models.decisions[A.title] = decision("other")
    models.users.clear()

    again = await in_read(conn, replay.resume, CONFIG, r.eval_run_id)
    assert again.stale == {A.id}
    assert [p.id for p in again.to_classify] == [A.id] and again.to_grade == [(A.id, TOPIC)]
    await replay.run(db, CONFIG, again, models=models.gate(CONFIG))
    # Both stages read the new text, and the row says which.
    asked = [u for u in models.users if A.title in u]
    assert len(asked) == 3 and all(rewritten.abstract in u for u in asked)  # classify, judge, check
    (row,) = [
        x for x in await in_read(conn, evaluation.results, r.eval_run_id) if x["paper_id"] == A.id
    ]
    assert (row["paper_revision"], row["classifier"], row["fit"]) == (2, False, "related")
    assert (await in_read(conn, replay.resume, CONFIG, r.eval_run_id)).stale == set()


async def test_the_estimate_goes_by_the_journal_and_counts_what_is_left(conn: Session, models):
    r = await in_read(conn, replay.prepare, LABELLED)
    assert (len(r.to_classify), len(r.to_grade)) == (3, 3)
    first = await in_read(conn, replay.estimate, CONFIG, r)
    assert first == pytest.approx(
        {"classification": 3 * 0.0006, "grading": 3 * (0.003 + 0.0006)}
    )  # no calls yet to go by
    await start(conn, models)
    again = await in_read(conn, replay.estimate, CONFIG, r)  # every scripted call cost $0.001
    assert again == pytest.approx({"classification": 3 * 0.001, "grading": 3 * 0.002})
    # Calls under another effort cost differently: the judge at high has none to go by yet.
    judge = CONFIG.models.judge.model_copy(update={"effort": "high"})
    harder = CONFIG.model_copy(update={"models": CONFIG.models.model_copy(update={"judge": judge})})
    assert (await in_read(conn, replay.estimate, harder, r))["grading"] == pytest.approx(
        3 * (0.003 + 0.001)
    )
    # A call a batch did not answer, sent normally on its row, is a normal call to go by.
    await conn.execute("UPDATE inference_calls SET batch_id = 'msgbatch_1'")
    assert await in_read(conn, replay.estimate, CONFIG, r) == pytest.approx(again)
    # Calls answered in a batch, at half price, are not what a replay's normal calls go by.
    await conn.execute('UPDATE inference_calls SET usage = usage || \'{"service_tier": "batch"}\'')
    assert await in_read(conn, replay.estimate, CONFIG, r) == pytest.approx(first)


async def test_quality_shows_recall_from_the_latest_replay_and_the_report_names_the_losses(
    conn: Session, models
):
    assert await in_read(conn, quality.replay_recall) is None
    models.decisions[A.title] = decision("cot-faithfulness")
    models.fits[("judge", B.title)] = {"reason": "Off.", "fit": "none"}
    models.fits[("check", B.title)] = {"reason": "Off.", "fit": "adjacent"}
    r = await start(conn, models)
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, "t", Config())
    async with AppClient(
        app,
        base_url="http://t",
        headers={"authorization": "Bearer t"},
    ) as client:
        page = (await client.get("/api/settings/quality")).json()
    shown = page["recall"]
    assert shown["eval_run"] == r.eval_run_id and shown["cost"] > 0
    owner = next(x for x in shown["recall"] if x["cohort"] == "owner")
    assert (owner["topic"], owner["positives"]) == (TOPIC, 2)
    assert owner["end_to_end"] == {"hits": 1, "n": 2}

    run = await in_read(conn, evaluation.get, r.eval_run_id)
    outcomes = await in_read(conn, evaluation.outcomes, r.eval_run_id)
    text = replay.report(
        run, recall(outcomes), outcomes, await in_read(conn, evaluation.results, r.eval_run_id)
    )
    assert "| owner | cot-faithfulness | 2 | 0.50 (1/2) | 1.00 (2/2) | 0.50 (1/2) |" in text
    assert "a member; graded adjacent (judge none, check adjacent)" in text


# -- a replay as a job (docs/plans/jobs.md, P3: V6) ---------------------------------------------

SCHEDULED = CONFIG.model_copy(update={"schedule": QUIET})  # the worker runs, no fire near
FIT_HEAD = "paper_id,topic,fit,legacy,cohort,origin,labelled_at,note\n"


@pytest.fixture
def labels(tmp_path, monkeypatch):
    """Label files of the owner's three labels and one legacy label, where a job reads them."""
    (tmp_path / "fit.csv").write_text(
        FIT_HEAD
        + "".join(
            f"{p},{TOPIC},{fit},,owner,gold,2026-10-09,\n"
            for p, fit in ((A.id, "central"), (B.id, "central"), (C.id, "none"))
        )
    )
    regression = f"paper_id,topic,tier,source\n{A.id},{TOPIC},4,legacy\n"
    (tmp_path / "regression.csv").write_text(regression)
    monkeypatch.setattr(replay, "LABELS", tmp_path)


@pytest.fixture
async def priced(conn: Session, monkeypatch, labels):
    """The taxonomy, the three papers, and every replay role with a price bound: the Anthropic
    judge at a listed price, the OpenRouter classifier and check by their charged history. Each
    call costs `test_reruns.COST`."""
    await settling.save_tree(conn, TREE)
    await in_write(conn, papers_store.upsert, [A, B, C])
    monkeypatch.setitem(prices.ANTHROPIC, "test-judge", (1.0, 2.0, 0.1))
    for model in ("test/decider", "test/check"):
        await charged(conn, model, cost=0.0003, tokens_in=1000, tokens_out=1000)
    return Priced()


@asynccontextmanager
async def api(conn: Session, config=SCHEDULED):
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, "t", config)
    headers = {"authorization": "Bearer t"}
    async with AppClient(app, base_url="http://t", headers=headers) as c:
        yield c


async def run_replay(conn: Session, ceiling: float = 5.0, config=SCHEDULED) -> dict:
    """Run replay from Overview: the job it queued or answered with."""
    async with api(conn, config) as http:
        answer = await http.post("/api/jobs/replays", json={"ceiling": ceiling})
    assert answer.status_code == 201, answer.text
    (job,) = [j for j in answer.json()["running"] if j["kind"] == "replay"]
    return job


async def test_a_replay_job_from_overview_writes_only_to_the_evaluation_tables(
    conn: Session, db, priced
):
    async with Bridge(conn).write("report", taxonomy="shared", papers=[C.id]) as unit:
        await misses_feedback.report(unit, C.id, TOPIC, Fit.CENTRAL)
    before = await contents(conn)
    async with api(conn) as http:
        offer = (await http.get("/api/jobs/replays/offer")).json()
        assert offer["labels"] == {"owner": 3, "legacy": 1, "miss": 1}
        assert (offer["papers"], offer["pairs"], offer["running"]) == (3, 3, None)
        assert offer["estimate"] > 0 and offer["unpriced"] == [] and offer["continues"] is None
        job = await run_replay(conn)
        assert (job["state"], job["papers"], job["ceiling"]) == ("queued", 3, 5.0)
        # Asked again while it waits (a lost reply, a second click), it only answers with it.
        assert (await run_replay(conn))["id"] == job["id"]
        assert (await http.get("/api/jobs/replays/offer")).json()["running"]["id"] == job["id"]

    await work_out(worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED)))
    after = await contents(conn)
    changed = {t for t in before if before[t] != after[t]}
    # Its job, its chunks' work items, run and call journal; nothing of the pipeline moves,
    # verdicts and the outbox included, nor what the status document reads of the providers.
    evaluated = {"eval_runs", "eval_labels", "eval_results", "runs", "inference_calls"}
    assert changed == evaluated | {"jobs", "work_items"}
    done = await in_read(conn, jobs_store.get, job["id"])
    assert (done["state"], done["done"], done["counts"]["graded"]) == ("done", 3, 3)
    shown = await in_read(conn, quality.replay_recall)
    assert shown["eval_run"] == done["scope"]["eval_run"] and shown["finished_at"] is not None
    owner = next(x for x in shown["recall"] if x.cohort == "owner")
    assert (owner.positives, owner.grading.n) == (2, 2)
    (run,) = await rows(conn, "SELECT kind FROM runs WHERE job_id = %s", job["id"])
    assert run["kind"] == "eval"  # an evaluation's run, carried by the job: its cost is the job's


async def test_a_replay_chunk_tried_again_after_a_lost_database_counts_what_it_graded(
    conn: Session, db, priced, monkeypatch
):
    """The database goes away after a chunk's papers are graded, before its progress is
    recorded: the gate tries it again, which finds them concluded and grades none, yet the job
    counts what the first try graded."""
    import psycopg  # noqa: PLC0415

    job = await run_replay(conn)
    real, lost = replay.work, []

    async def drops(*args, **kwargs):
        await real(*args, **kwargs)
        if not lost:
            lost.append(True)
            raise psycopg.OperationalError("the server closed the connection unexpectedly")

    monkeypatch.setattr(replay, "work", drops)
    await work_out(worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED)))
    done = await job_of(conn, job["id"])
    assert lost and (done["state"], done["done"], done["counts"]["graded"]) == ("done", 3, 3)


@pytest.mark.parametrize(
    ("put", "counted"), [("put_classified", "classified"), ("put_graded", "graded")]
)
async def test_a_replay_result_that_rolled_back_is_counted_once_when_tried_again(
    conn: Session, db, priced, monkeypatch, put, counted
):
    """The database goes away as a stage's first result is written, which rolls back: the gate
    tries the chunk again, which concludes that paper again, and the job counts it once."""
    import psycopg  # noqa: PLC0415

    job = await run_replay(conn)
    real, lost = getattr(evaluation, put), []

    async def drops(unit, *args, **kwargs):
        await real(unit, *args, **kwargs)
        if not lost:
            lost.append(True)
            raise psycopg.OperationalError("the server closed the connection unexpectedly")

    monkeypatch.setattr(evaluation, put, drops)
    await work_out(worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED)))
    done = await job_of(conn, job["id"])
    assert lost and (done["state"], done["done"], done["counts"][counted]) == ("done", 3, 3)


async def test_a_replay_job_taken_back_goes_on_without_redoing_what_it_concluded(
    conn: Session, db, priced, monkeypatch
):
    monkeypatch.setattr(worker, "CHUNK", 1)
    job = await run_replay(conn)
    async with gate_on(conn) as dead:  # another process's
        gone = worker.JobWorker(dead, SCHEDULED, priced.gate(SCHEDULED))
        assert await step(gone)  # A's chunk
        await killed(gone, monkeypatch, "_replay")  # its process dies at the next
        await gone.close(RuntimeError("killed"))

    await work_out(worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED)))
    done = await in_read(conn, jobs_store.get, job["id"])
    assert (done["state"], done["done"]) == ("done", 3)
    assert sorted(priced.sent("classify")) == sorted([A.title, B.title, C.title])
    assert sorted(priced.sent("judge")) == sorted([A.title, B.title, C.title])
    run = await in_read(conn, evaluation.get, done["scope"]["eval_run"])
    assert len(run["run_ids"]) == 2 and run["finished_at"] is not None  # both runs' cost


async def test_a_replay_job_stops_at_its_ceiling_and_run_replay_goes_on_with_it(
    conn: Session, db, priced
):
    """No call fits under a ceiling of a millionth of a dollar: none is sent, nothing is
    recorded as concluded, and the job waits under Not run saying where. Run replay goes on
    with it under a new ceiling, and concludes every paper."""
    job = await run_replay(conn, ceiling=1e-6)
    await work_out(worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED)))
    stopped = await in_read(conn, jobs_store.get, job["id"])
    assert stopped["state"] == "stopped"
    assert stopped["error"] == "stopped at the ceiling after 0 of 3"
    assert priced.sent("classify") == [] and priced.sent("judge") == []
    eval_run = stopped["scope"]["eval_run"]
    assert await in_read(conn, evaluation.results, eval_run) == []

    async with api(conn) as http:
        offer = (await http.get("/api/jobs/replays/offer")).json()
    assert (offer["continues"], offer["papers"], offer["pairs"]) == (job["id"], 3, 3)
    assert (await run_replay(conn, ceiling=5.0))["id"] == job["id"]
    await work_out(worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED)))
    done = await in_read(conn, jobs_store.get, job["id"])
    assert (done["state"], done["error"], float(done["ceiling"])) == ("done", None, 5.0)
    assert done["scope"]["eval_run"] == eval_run
    assert sorted(priced.sent("classify")) == sorted([A.title, B.title, C.title])
    assert await in_read(conn, jobs_store.spent, job["id"]) == pytest.approx(
        9 * COST
    )  # 3 papers, 3 calls


async def test_a_replay_jobs_ceiling_covers_all_its_chunks(conn: Session, db, priced, monkeypatch):
    """A chunk a paper, under a ceiling one paper's calls fit in. The first chunk runs, then the
    service stops; the next chunk, another process's on a run of its own, has what is left of
    the ceiling after the first's spend, which no second paper fits in: the job stops."""
    monkeypatch.setattr(worker, "CHUNK", 1)
    job = await run_replay(conn, ceiling=7 * COST)
    first = worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED))
    assert await step(first)
    await first.close(RuntimeError("the service stopped"))
    await work_out(worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED)))
    stopped = await job_of(conn, job["id"])
    assert (stopped["state"], stopped["error"]) == (
        "stopped",
        "stopped at the ceiling after 1 of 3",
    )
    spent = await in_read(conn, jobs_store.spent, job["id"])
    assert 3 * COST <= spent <= 7 * COST  # the second's calls fit what the first left
    executions = await rows(conn, "SELECT id FROM runs WHERE job_id = %s", job["id"])
    assert len(executions) == 2


async def test_a_replay_jobs_ceiling_covers_its_chunks_in_one_serve(
    conn: Session, db, priced, monkeypatch
):
    """A chunk a paper, under a ceiling one paper's calls fit in, every chunk run by the one
    job Worker of one `serve`, on the run it keeps: the second chunk has what the first left,
    which no second paper fits in, and the job stops."""
    monkeypatch.setattr(worker, "CHUNK", 1)
    job = await run_replay(conn, ceiling=7 * COST)

    async def stopped() -> bool:
        return (await job_of(conn, job["id"]))["state"] == "stopped"

    quiet = SCHEDULED.model_copy(update={"schedule": QUIET})
    async with served(db, quiet, models=priced.gate(quiet)):
        await until(stopped, "the job to stop at its ceiling")
    found = await job_of(conn, job["id"])
    assert found["error"] == "stopped at the ceiling after 1 of 3"
    spent = await in_read(conn, jobs_store.spent, job["id"])
    assert 3 * COST <= spent <= 7 * COST
    assert len(priced.sent("judge")) == 1  # the second paper's calls never sent
    executions = await rows(conn, "SELECT id FROM runs WHERE job_id = %s", job["id"])
    assert len(executions) == 1  # one run, kept across its chunks


async def test_a_replay_jobs_ceiling_holds_as_its_chunks_go_back_and_forth(
    conn: Session, db, priced, monkeypatch
):
    """A chunk a paper, under a ceiling two papers' calls fit in. One process runs the first
    chunk, another the second, then the first the third: the run it kept is no longer the
    job's (the other's ran since), so it is closed, and the third chunk opens one under what
    the ceiling has left after both, which no third paper fits in: the job stops within it."""
    monkeypatch.setattr(worker, "CHUNK", 1)
    # A paper's calls spend 3 COST and need more than 4 COST left to be sent (the test above).
    job = await run_replay(conn, ceiling=10 * COST)
    first = worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED))
    async with gate_on(conn) as elsewhere:
        other = worker.JobWorker(elsewhere, SCHEDULED, priced.gate(SCHEDULED))
        assert await step(first)
        assert await step(other)
        assert await step(first)
        await other.close(RuntimeError("the service stopped"))
    stopped = await job_of(conn, job["id"])
    assert (stopped["state"], stopped["error"]) == (
        "stopped",
        "stopped at the ceiling after 2 of 3",
    )
    assert await in_read(conn, jobs_store.spent, job["id"]) <= 10 * COST
    executions = await rows(conn, "SELECT * FROM runs WHERE job_id = %s ORDER BY id", job["id"])
    assert len(executions) == 3 and all(r["finished_at"] for r in executions)
    assert first.open == {}


async def test_serve_runs_a_replay_as_a_worker(conn: Session, db, priced):
    """Through `serve`'s own lifespan: Run replay's job is swept, run by the gate, and done."""
    job = await run_replay(conn)

    async def done() -> bool:
        return (await job_of(conn, job["id"]))["state"] == "done"

    quiet = SCHEDULED.model_copy(update={"schedule": QUIET})
    async with served(db, quiet, models=priced.gate(quiet)):
        await until(done, "the replay to be done")
    assert sorted(priced.sent("judge")) == sorted([A.title, B.title, C.title])


async def test_a_stopped_replay_that_no_longer_resumes_gives_way_to_a_new_one(
    conn: Session, db, priced
):
    job = await run_replay(conn, ceiling=1e-6)
    await work_out(worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED)))
    stricter = SCHEDULED.model_copy(update={"classification": Classification(focus_threshold=0.6)})
    async with api(conn, stricter) as http:
        assert (await http.get("/api/jobs/replays/offer")).json()["continues"] is None
    made = await run_replay(conn, ceiling=5.0, config=stricter)
    assert made["id"] != job["id"]
    assert (await in_read(conn, jobs_store.get, job["id"]))["state"] == "discarded"


async def test_run_replay_needs_the_worker(conn: Session, priced):
    async with api(conn, CONFIG) as http:
        off = await http.post("/api/jobs/replays", json={"ceiling": 5.0})
    assert off.status_code == 409 and "schedule is off" in off.json()["detail"]
    assert await rows(conn, "SELECT id FROM jobs") == []


async def test_try_again_on_a_failed_replay_waits_while_another_is_under_way(conn: Session, priced):
    first = await run_replay(conn)
    await conn.execute("UPDATE jobs SET state = 'failed' WHERE id = %s", (first["id"],))
    second = await run_replay(conn)
    assert second["id"] != first["id"]
    async with api(conn) as http:
        refused = await http.post(f"/api/jobs/{first['id']}/again")
        assert refused.status_code == 409 and "another replay" in refused.json()["detail"]
        await conn.execute("UPDATE jobs SET state = 'done' WHERE id = %s", (second["id"],))
        assert (await http.post(f"/api/jobs/{first['id']}/again")).status_code == 200


async def test_a_paper_revised_between_chunks_is_replayed_on_its_new_text(
    conn: Session, db, priced, monkeypatch
):
    monkeypatch.setattr(worker, "CHUNK", 1)
    job = await run_replay(conn)
    w = worker.JobWorker(db, SCHEDULED, priced.gate(SCHEDULED))
    assert await step(w)  # A's chunk
    revised = A.model_copy(update={"abstract": "Sparse autoencoders on activations."})
    await in_write(conn, papers_store.upsert, [revised])
    await work_out(w)
    done = await in_read(conn, jobs_store.get, job["id"])
    assert (done["state"], done["done"]) == ("done", 3)
    assert priced.sent("classify").count(A.title) == 2


async def test_a_replay_leaves_a_dead_nightlys_batch_alone(conn: Session, db, priced):
    """A batch a nightly left when its process died is the next run's to account for, not a
    replay's: a dry run neither cancels it nor answers from it."""
    role = SCHEDULED.models.judge
    request = grading.request(A, FAITH, TREE.get(FAITH.slug).name)
    batching = BatchingProvider("anthropic", priced.respond, hold=True)

    providers = {"anthropic": batching, "openrouter": Costed("openrouter", priced.respond)}
    gate = ModelGate(SCHEDULED.models, providers, CallJournal(db), retry_delays=(0.0,))

    key = gate.on().request_key(role, request)
    batch = await batching.submit([(key, role, request)])
    nightly = await started(conn, "nightly")
    journal = CallJournal(db, nightly)
    start = CallStart(
        run_id=nightly, work_id=None, operation="judge", model=role.model, key=key,
        paper_id=A.id, via=role.provider,
    )  # fmt: skip
    call = await journal.start(start)
    await journal.batched([call], batch, role.provider)
    await in_write(conn, runs.finish, nightly, error="process died")

    await run_replay(conn)
    await work_out(worker.JobWorker(db, SCHEDULED, gate))
    assert batching.cancelled == []
    (left,) = await rows(conn, "SELECT output FROM inference_calls WHERE id = %s", call)
    assert left["output"] is None
