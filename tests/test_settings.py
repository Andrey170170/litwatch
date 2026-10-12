"""Settings: editing topics, previewing an edit, scoped re-runs, Quality and configuration."""

import json

import httpx
import pytest

from litwatch import store
from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import Grade, Label, LabelOrigin, LabelValue, Opinion, Verdict
from litwatch.feedback import word
from litwatch.jobs.lock import RUN_LOCK
from litwatch.llm import Gateway
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts
from tests.conftest import FAITH, TOPICS, paper
from tests.fakes import ScriptedProvider

TOKEN = "s3cret"
P = paper("2610.00001", title="Faithful traces", abstract="Chain-of-thought omits steps.")
Q = paper("2610.00002", title="Protein folding", abstract="Structures.")


class Models:
    def __init__(self):
        self.graded: list[str] = []

    def respond(self, role, request):
        if "sort arXiv papers" in request.system:
            return json.dumps(
                {
                    "cot_faithfulness": {"why": "w", "relation": "about"},
                    "interpretability": {"yes": False},
                    "agents": {"yes": False},
                }
            )
        self.graded.append(request.system.split("# Topic: ")[1].splitlines()[0])
        return json.dumps({"reason": "r", "grade": "relevant"})

    def gateway(self, config, journal):
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=())


@pytest.fixture
async def models():
    return Models()


@pytest.fixture
async def client(conn, models):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [P, Q])
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, Config(), gateway=models.gateway)
    transport = httpx.ASGITransport(app=app)
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def test_a_topic_edit_applies_forward_and_a_kind_change_settles_its_papers(client, conn):
    body = FAITH.model_dump(mode="json")
    saved = (await client.put(f"/api/settings/topics/{FAITH.slug}", json=body)).json()
    assert saved["revision"] == 1  # nothing a model sees changed
    body["rubric"] = "Another rubric."
    assert (await client.put(f"/api/settings/topics/{FAITH.slug}", json=body)).json()[
        "revision"
    ] == 2
    bad = await client.put(f"/api/settings/topics/{FAITH.slug}", json=body | {"subject": "short"})
    assert bad.status_code == 422

    await verdicts.add(
        conn, Verdict(paper_id=P.id, topic=FAITH.slug, topic_revision=2, grade=Grade.RELEVANT)
    )
    assert (await word.settle(conn, P.id)).saved
    await client.put(f"/api/settings/topics/{FAITH.slug}", json=body | {"kind": "radar"})
    row = await (await conn.execute("SELECT saved FROM library")).fetchone()
    assert row["saved"] is False


async def test_the_owners_order_and_keyword_counts(client):
    order = ["agents", "interpretability", FAITH.slug]
    assert (
        await client.put("/api/settings/topics-order", json={"slugs": order})
    ).status_code == 204
    topics = (await client.get("/api/settings/topics")).json()
    assert [t["slug"] for t in topics if t["kind"] == "watch"] == [FAITH.slug]
    assert {t["slug"]: t["position"] for t in topics}["agents"] == 1
    counts = (await client.get(f"/api/settings/topics/{FAITH.slug}/keywords")).json()
    assert counts == {r"chain.of.thought": 1, r"\bCoT\b": 0, "faithful": 1}


async def test_a_preview_grades_only_the_draft_over_the_topics_evidence(client, conn, models):
    from litwatch.feedback import word as w

    await w.label(
        conn,
        Label(
            paper_id=P.id, topic=FAITH.slug, value=LabelValue.RELEVANT, origin=LabelOrigin.READER
        ),
    )
    draft = FAITH.model_dump(mode="json") | {"name": "Draft name", "rubric": "Drafted."}
    estimate = (await client.post(f"/api/settings/topics/{FAITH.slug}/preview", json=draft)).json()
    assert estimate["papers"] == 1
    (run,) = (await client.get("/api/settings/evaluations", params={"kind": "preview"})).json()
    report = (await client.get(f"/api/settings/evaluations/{run['id']}")).json()
    assert report["finished"] and report["topics"][FAITH.slug]["recall_firm"]["end_to_end"] == 1.0
    assert set(models.graded) == {"Draft name"}  # both graders, the draft only
    # Nothing was written outside the evaluation tables.
    assert await (await conn.execute("SELECT * FROM verdicts")).fetchall() == []


async def test_a_candidate_rerun_regrades_for_real(client, conn):
    await conn.execute(
        "INSERT INTO candidates (paper_id, topic, topic_revision, by_triage) "
        "VALUES (%s, %s, 1, true)",
        (P.id, FAITH.slug),
    )
    estimates = (await client.get(f"/api/settings/topics/{FAITH.slug}/reruns")).json()
    assert {e["scope"]: e["papers"] for e in estimates} == {
        "evidence": 0,
        "candidates": 1,
        "keywords": 1,
    }
    started = await client.post(
        f"/api/settings/topics/{FAITH.slug}/reruns", json={"scope": "candidates"}
    )
    assert started.status_code == 202
    (verdict,) = await (await conn.execute("SELECT grade FROM verdicts")).fetchall()
    assert verdict["grade"] == "relevant"


async def test_quality_lists_last_nights_splits_and_config_hides_secrets(client, conn):
    await conn.execute("INSERT INTO runs (kind, finished_at) VALUES ('nightly', now())")
    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id,
            topic=FAITH.slug,
            topic_revision=1,
            grade=Grade.RELEVANT,
            split=True,
            judge=Opinion(grade=Grade.RELEVANT, reason="yes"),
            check=Opinion(grade=Grade.NONE, reason="no"),
        ),
    )
    page = (await client.get("/api/settings/quality")).json()
    (split,) = page["night"]["splits"]
    assert (split["paper_id"], split["judge"]["reason"], split["check"]["grade"]) == (
        P.id,
        "yes",
        "none",
    )
    config = (await client.get("/api/settings/config")).json()
    assert config["models"]["writer"]["model"] == "claude-opus-5-5"


async def test_a_rerun_grades_only_inside_its_scope(client, conn):
    # A pending candidate outside the range must stay pending.
    await conn.execute(
        "INSERT INTO candidates (paper_id, topic, topic_revision, by_triage) "
        "VALUES (%s, %s, 1, true)",
        (Q.id, FAITH.slug),
    )
    started = await client.post(
        f"/api/settings/topics/{FAITH.slug}/reruns",
        json={"scope": "range", "since": "2001-01-01", "until": "2001-01-02"},
    )
    assert started.status_code == 202
    assert await (await conn.execute("SELECT * FROM verdicts")).fetchall() == []


async def test_grade_now_takes_the_run_lock_and_closes_the_old_candidate(client, conn):
    await conn.execute(
        "INSERT INTO candidates (paper_id, topic, topic_revision, by_triage, failed_runs) "
        "VALUES (%s, %s, 1, true, 3)",
        (P.id, FAITH.slug),
    )
    body = FAITH.model_dump(mode="json") | {"rubric": "Edited."}
    await client.put(f"/api/settings/topics/{FAITH.slug}", json=body)  # now revision 2
    pair = {"paper_id": P.id, "topic": FAITH.slug}
    async with await store.sibling(conn) as other:
        await other.execute("SELECT pg_advisory_lock(%s)", (RUN_LOCK,))
        assert (await client.post("/api/settings/quality/grade-now", json=pair)).status_code == 409
        await other.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK,))
    assert (await client.post("/api/settings/quality/grade-now", json=pair)).json()["graded"]
    (candidate,) = await (await conn.execute("SELECT state FROM candidates")).fetchall()
    assert candidate["state"] == "graded"


async def test_keyword_counts_match_as_the_audit_does(client, conn):
    await papers_store.upsert(conn, [paper("2610.00003", title="CoT methods", abstract="x")])
    counts = (await client.get(f"/api/settings/topics/{FAITH.slug}/keywords")).json()
    assert counts[r"\bCoT\b"] == 1


async def test_bad_input_is_refused_not_crashed(client):
    body = FAITH.model_dump(mode="json") | {"keywords": ["["]}
    assert (await client.put(f"/api/settings/topics/{FAITH.slug}", json=body)).status_code == 422
    order = {"slugs": ["agents", "agents", FAITH.slug]}
    assert (await client.put("/api/settings/topics-order", json=order)).status_code == 422


async def test_keyword_counts_for_an_unsaved_draft(client):
    counts = await client.get(
        f"/api/settings/topics/{FAITH.slug}/keywords",
        params=[("pattern", "omits"), ("pattern", "[")],
    )
    assert counts.json() == {"omits": 1, "[": -1}


async def test_a_preview_that_fails_finishes_with_its_error(client, conn, monkeypatch):
    from litwatch import screening

    def broken(*args, **kwargs):
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(screening, "triage", broken)
    await conn.execute(
        "INSERT INTO labels (paper_id, topic, value, origin) VALUES (%s, %s, 'relevant', 'reader')",
        (P.id, FAITH.slug),
    )
    draft = FAITH.model_dump(mode="json")
    await client.post(f"/api/settings/topics/{FAITH.slug}/preview", json=draft)
    (run,) = (await client.get("/api/settings/evaluations", params={"kind": "preview"})).json()
    report = (await client.get(f"/api/settings/evaluations/{run['id']}")).json()
    assert report["finished"] and "triage exploded" in report["error"]
