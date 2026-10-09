"""Evaluation as a dry run: what replay measures, what it must not touch, and the sample."""

import json

import pytest

from litwatch import feedback
from litwatch.config import Config
from litwatch.core import Grade, Label, LabelOrigin, LabelValue
from litwatch.feedback import seed
from litwatch.llm import Gateway
from litwatch.store import feedback as feedback_store
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from tests.conftest import FAITH, TOPICS, paper
from tests.fakes import ScriptedProvider

# What each paper is, which the scripted models read off the title:
#   found      triage says yes, graders say relevant
#   keyworded  triage says no, the keyword audit catches it, graders say relevant
#   dropped    triage says no and no keyword matches, though graders would say relevant
#   harsh      triage says yes, graders say adjacent
#   noise      triage says yes, graders say relevant; the owner says it is not
PAPERS = {
    "found": paper("2609.10001", title="found", abstract="Omitted computation in traces."),
    "keyworded": paper("2609.10002", title="keyworded", abstract="Chain-of-thought study."),
    "dropped": paper("2609.10003", title="dropped", abstract="Hidden steps of a model."),
    "harsh": paper("2609.10004", title="harsh", abstract="Verbalised hints."),
    "noise": paper("2609.10005", title="noise", abstract="Summaries of news."),
}


def respond(role, request):
    title = request.user.splitlines()[0].removeprefix("Title: ")
    if "sort arXiv papers" in request.system:
        return json.dumps(
            {
                "cot_faithfulness": {
                    "why": "w",
                    "relation": "about" if title in ("found", "harsh", "noise") else "unrelated",
                },
                "interpretability": {"yes": title == "dropped"},
                "agents": {"yes": False},
            }
        )
    return json.dumps({"reason": "r", "grade": "adjacent" if title == "harsh" else "relevant"})


def gateway(config, journal):
    providers = {n: ScriptedProvider(n, respond) for n in ("anthropic", "openrouter")}
    return Gateway(config.models, providers, journal, retry_delays=())


async def label(conn, name, value, origin=LabelOrigin.INBOX, provisional=False):
    made = Label(paper_id=PAPERS[name].id, topic=FAITH.slug, value=LabelValue(value), origin=origin)
    if provisional:
        await feedback_store.add_provisional(conn, made)
    else:
        await feedback_store.set_label(conn, made)


@pytest.fixture
async def seeded(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, list(PAPERS.values()))
    for name in ("found", "keyworded", "dropped", "harsh"):
        await label(conn, name, "relevant")
    await label(conn, "noise", "not_relevant", LabelOrigin.CANDIDATE_SAMPLE)
    return conn


async def test_replay_reports_four_recalls_over_the_papers_labelled_relevant(seeded):
    conn = seeded
    papers, labelled = await feedback.replay_set(conn)
    eval_run_id = await feedback.replay(conn, Config(), papers, grade=labelled, gateway=gateway)
    report = await feedback.metrics(conn, eval_run_id)
    recall = report["topics"][FAITH.slug]["recall_firm"]
    assert recall["n"] == 4
    # The keyword audit rescues one paper, and that must not flatter the triage model.
    assert (recall["triage"], recall["candidate"]) == (0.5, 0.75)
    # Every labelled paper is graded, so the dropped one shows grading would have kept it.
    assert (recall["grading"], recall["end_to_end"]) == (0.75, 0.5)
    assert recall["triage_misses"] == [PAPERS["keyworded"].id, PAPERS["dropped"].id]
    assert recall["candidate_misses"] == [PAPERS["dropped"].id]
    assert recall["grading_misses"] == [PAPERS["harsh"].id]
    # Precision is kept apart by how the labelled paper was chosen.
    assert report["topics"][FAITH.slug]["precision_by_origin"] == {
        "candidate_sample": {"surfaced": 1, "precision": 0.0},
        "inbox": {"surfaced": 2, "precision": 1.0},
    }
    assert report["models"]["check"] == "openrouter:meta/muse-spark-1.3-contributor@high"
    assert report["calls"]["judge"]["calls"] == 5


async def test_replay_writes_to_evaluation_tables_and_nothing_else(seeded):
    conn = seeded
    before = await (await conn.execute("SELECT count(*) AS n FROM outbox")).fetchone()
    papers, labelled = await feedback.replay_set(conn)
    await feedback.replay(conn, Config(), papers, grade=labelled, gateway=gateway)
    for table in ("triage", "memberships", "candidates", "verdicts"):
        assert await (await conn.execute(f"SELECT * FROM {table}")).fetchall() == []
    assert await (await conn.execute("SELECT count(*) AS n FROM outbox")).fetchone() == before
    assert len(await feedback_store.results(conn, 1)) == len(PAPERS) * len(TOPICS)


async def test_provisional_labels_are_reported_apart_and_unsure_counts_nowhere(seeded):
    conn = seeded
    await feedback_store.delete_label(conn, PAPERS["dropped"].id, FAITH.slug)
    await label(conn, "dropped", "relevant", LabelOrigin.LEGACY_POSITIVE, provisional=True)
    await label(conn, "harsh", "unsure")
    papers, labelled = await feedback.replay_set(conn)
    assert PAPERS["harsh"].id not in labelled
    eval_run_id = await feedback.replay(conn, Config(), papers, grade=labelled, gateway=gateway)
    topic = (await feedback.metrics(conn, eval_run_id))["topics"][FAITH.slug]
    assert (topic["recall_firm"]["n"], topic["recall_firm"]["triage"]) == (2, 0.5)
    assert (topic["recall_provisional"]["n"], topic["recall_provisional"]["candidate"]) == (1, 0.0)
    # The owner's own label replaces a provisional one and keeps how the paper was chosen.
    await label(conn, "dropped", "not_relevant")
    (kept,) = [x for x in await feedback_store.labels(conn) if x.paper_id == PAPERS["dropped"].id]
    assert (kept.value, kept.provisional, kept.origin) == (
        LabelValue.NOT_RELEVANT,
        False,
        LabelOrigin.LEGACY_POSITIVE,
    )
    # A bulk import never replaces a label that is already there.
    await label(conn, "dropped", "relevant", LabelOrigin.LEGACY_POSITIVE, provisional=True)
    assert (await feedback_store.labels(conn))[2].value == LabelValue.NOT_RELEVANT


async def test_regression_papers_are_checked_for_becoming_candidates_without_being_graded(seeded):
    conn = seeded
    extra = paper("2609.10009", title="found", abstract="Another one.")
    lost = paper("2609.10010", title="dropped", abstract="Nothing matching.")
    await papers_store.upsert(conn, [extra, lost])
    await feedback_store.add_regression(conn, extra.id, FAITH.slug, 2, "legacy")
    await feedback_store.add_regression(conn, lost.id, FAITH.slug, 1, "legacy")
    papers, labelled = await feedback.replay_set(conn)
    eval_run_id = await feedback.replay(conn, Config(), papers, grade=labelled, gateway=gateway)
    report = await feedback.metrics(conn, eval_run_id)
    regression = report["topics"][FAITH.slug]["regression"]
    assert regression["legacy_tier_2"] == {"n": 1, "candidates": 1, "by_triage": 1, "lost": []}
    assert regression["legacy_tier_1"]["lost"] == [lost.id]
    assert report["calls"]["judge"]["calls"] == len(PAPERS)


async def test_the_reject_sample_is_stratified_seeded_and_reproducible(seeded):
    conn = seeded
    more = [paper(f"2609.2{i:04d}", title="dropped", abstract="x") for i in range(6)]
    more += [paper(f"2609.3{i:04d}", title="other", abstract="y") for i in range(6)]
    await papers_store.upsert(conn, more)

    async def draw(seed_):
        eval_run_id = await feedback.sample_rejects(
            conn, Config(), seed=seed_, pool=100, near=3, far=2, gateway=gateway
        )
        return eval_run_id, await feedback_store.sample(conn, eval_run_id)

    first_id, first = await draw(7)
    assert sorted(r["stratum"] for r in first) == ["far", "far", "near", "near", "near"]
    titles = {p.id: p.title for p in [*more, *PAPERS.values()]}
    # Triage said yes to none of them; "near" ones sit in a Radar field.
    assert all(titles[r["paper_id"]] in ("dropped", "other", "keyworded") for r in first)
    assert all(titles[r["paper_id"]] == "dropped" for r in first if r["stratum"] == "near")
    _, again = await draw(7)
    assert [r["paper_id"] for r in again] == [r["paper_id"] for r in first]
    _, other = await draw(8)
    assert [r["paper_id"] for r in other] != [r["paper_id"] for r in first]
    # Nothing was graded, and the owner's findings in the sample are counted per stratum.
    report = await feedback.metrics(conn, first_id)
    assert "judge" not in report["calls"]
    near = next(r["paper_id"] for r in first if r["stratum"] == "near")
    await feedback_store.set_label(
        conn,
        Label(
            paper_id=near,
            topic=FAITH.slug,
            value=LabelValue.RELEVANT,
            grade=Grade.RELEVANT,
            origin=LabelOrigin.REJECT_SAMPLE,
        ),
    )
    summary = (await feedback.metrics(conn, first_id))["sample"]
    assert summary["near"]["relevant"] == [f"{near}:{FAITH.slug}"]
    assert (summary["near"]["drawn"], summary["far"]["drawn"]) == (3, 2)
    assert summary["near"]["labelled"] >= 1


async def test_the_0x_label_file_gives_labels_only_where_the_owner_labelled(seeded, tmp_path):
    conn = seeded
    path = tmp_path / "labeled.yaml"
    path.write_text(
        """
schema_version: 2
papers:
- id: '2501.00001'
  title: Gold positive
  abstract: A
  expected: {cot-faithfulness: 2, mech-interp: 1}
  note: 'gold: Andrei. | converted'
- id: '2501.00002'
  title: Gold negative
  abstract: B
  expected: {cot-faithfulness: 3}
  note: 'gold: Andrei.'
- id: '2501.00003'
  title: Silver
  abstract: C
  expected: {cot-faithfulness: 1}
  note: 'hand pass, awaiting Andrei'
"""
    )
    assert await seed.from_label_file(conn, path) == {"gold": 2, "silver": 1}
    made = {
        x.paper_id: x for x in await feedback_store.labels(conn) if x.origin == LabelOrigin.GOLD
    }
    assert (made["2501.00001"].value, made["2501.00001"].grade) == (
        LabelValue.RELEVANT,
        Grade.RELEVANT,
    )
    assert made["2501.00002"].value == LabelValue.NOT_RELEVANT
    assert [r["paper_id"] for r in await feedback_store.regression(conn)] == ["2501.00003"]
    assert await papers_store.get(conn, "2501.00003") is not None
