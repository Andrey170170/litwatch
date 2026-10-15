"""Settings: the taxonomy and editing topics, Quality, the schedule and configuration."""

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import Fit, Opinion, Verdict
from litwatch.feedback import word
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts
from tests.conftest import FAITH, TREE, paper

TOKEN = "s3cret"
P = paper("2610.00001", title="Faithful traces", abstract="Chain-of-thought omits steps.")
Q = paper("2610.00002", title="Protein folding", abstract="Structures.")


@pytest.fixture
async def client(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P, Q])
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, Config())
    transport = httpx.ASGITransport(app=app)
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


def _body(topic) -> dict:
    return topic.model_dump(mode="json", exclude={"slug"})


async def test_the_taxonomy_reads_back_as_saved(client):
    assert (await client.get("/api/settings/taxonomy")).json() == TREE.model_dump(
        mode="json", by_alias=True
    )


async def test_a_topic_edit_applies_forward_and_a_depth_change_settles_its_papers(client, conn):
    async def revision() -> int:
        row = await (
            await conn.execute("SELECT revision FROM topics WHERE class = %s", (FAITH.slug,))
        ).fetchone()
        return row["revision"]

    body = _body(FAITH)
    url = f"/api/settings/topics/{FAITH.slug}"
    saved = await client.put(url, json=body | {"colour": "#000000"})
    assert saved.json()["colour"] == "#000000" and await revision() == 1  # nothing graded changed
    body["subject"] = "A subject reworded: the class's description follows it."
    assert (await client.put(f"/api/settings/topics/{FAITH.slug}", json=body)).status_code == 200
    assert await revision() == 2
    tree = (await client.get("/api/settings/taxonomy")).json()
    (cls,) = [c for c in tree["classes"] if c["slug"] == FAITH.slug]
    assert cls["description"] == body["subject"]
    bad = await client.put(f"/api/settings/topics/{FAITH.slug}", json=body | {"subject": "short"})
    assert bad.status_code == 422
    nowhere = await client.put("/api/settings/topics/no-such-class", json=body)
    assert nowhere.status_code == 404

    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id, topic=FAITH.slug, topic_version="v", fit=Fit.RELATED, reason="r",
            opinions={},
        ),
    )  # fmt: skip
    assert (await word.settle(conn, P.id)).saved
    area = body | {"depth": "area", "rubric": None}
    assert (await client.put(f"/api/settings/topics/{FAITH.slug}", json=area)).status_code == 200
    row = await (await conn.execute("SELECT saved FROM library")).fetchone()
    assert row["saved"] is False


async def test_a_class_not_followed_can_be_followed(client):
    other = {"depth": "field", "monogram": "OT", "colour": "#111111"}
    other["subject"] = "Papers in the configured categories that fit nothing else."
    assert (await client.put("/api/settings/topics/other", json=other)).status_code == 200
    tree = (await client.get("/api/settings/taxonomy")).json()
    assert [t["class"] for t in tree["topics"]] == [*(t.slug for t in TREE.topics), "other"]


async def test_the_owners_order(client):
    order = ["agents", "interpretability", FAITH.slug]
    assert (
        await client.put("/api/settings/topics-order", json={"slugs": order})
    ).status_code == 204
    topics = (await client.get("/api/settings/taxonomy")).json()["topics"]
    assert [t["class"] for t in topics] == order


async def test_the_schedule_says_when_each_job_next_fires_in_its_zone(conn):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from litwatch.config import Schedule

    cfg = Config(schedule=Schedule(nightly="0 6 * * *", timezone="America/Denver"))
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    transport = httpx.ASGITransport(app=create_app(dsn, TOKEN, cfg))
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        out = (await c.get("/api/settings/schedule")).json()
    assert out["enabled"] is False and out["zone"] == "America/Denver"
    by_kind = {j["kind"]: j for j in out["jobs"]}
    assert set(by_kind) == {"nightly", "publications"}
    assert by_kind["nightly"]["cron"] == "0 6 * * *"
    denver = ZoneInfo("America/Denver")
    at = datetime.fromisoformat(by_kind["nightly"]["next"])
    assert at.tzinfo is not None and at > datetime.now(denver)
    local = at.astimezone(denver)
    assert (local.hour, local.minute) == (6, 0)


async def test_quality_lists_last_nights_splits_and_escalations_and_hides_secrets(client, conn):
    await conn.execute("INSERT INTO runs (kind, finished_at) VALUES ('nightly', now())")
    opinions = {
        "judge": Opinion(fit=Fit.CENTRAL, reason="yes", model="a"),
        "check": Opinion(fit=Fit.NONE, reason="no", model="b"),
    }
    marks = {FAITH.slug: {"split": True}, "interpretability": {"escalated": True}, "agents": {}}
    for topic, marked in marks.items():
        await verdicts.add(
            conn,
            Verdict(
                paper_id=P.id,
                topic=topic,
                topic_version="v",
                fit=Fit.CENTRAL,
                reason="yes",
                opinions=opinions,
                **marked,
            ),
        )
    page = (await client.get("/api/settings/quality")).json()
    split, escalated = sorted(page["night"]["splits"], key=lambda r: r["topic"])
    assert (split["topic"], split["opinions"]["check"]["fit"]) == (FAITH.slug, "none")
    assert (escalated["topic"], escalated["depth"]) == ("interpretability", "area")
    assert [c["provider"] for c in page["cost"]] == ["anthropic", "openrouter"]
    config = (await client.get("/api/settings/config")).json()
    assert config["models"]["writer"]["model"] == "claude-opus-5-5"


async def test_bad_input_is_refused_not_crashed(client):
    body = _body(FAITH) | {"keywords": ["["]}
    assert (await client.put(f"/api/settings/topics/{FAITH.slug}", json=body)).status_code == 422
    order = {"slugs": ["agents", "agents", FAITH.slug]}
    assert (await client.put("/api/settings/topics-order", json=order)).status_code == 422


async def test_settings_refuse_what_does_not_exist(client):
    assert (await client.post("/api/settings/calibrate/999/more")).status_code == 404


async def _classify(conn, paper, probabilities, primary):
    """Record a classification as the nightly step would, with the memberships it makes."""
    from litwatch import classification
    from litwatch.core.classification import Classified, memberships

    found = Classified(probabilities=probabilities, primary=primary)
    c = classification.Classification(
        paper=paper,
        taxonomy_revision=TREE.classes_version,
        versions={k.slug: k.version for k in TREE.classes},
        classified=found,
        memberships=memberships(TREE, found, paper.title, paper.abstract),
        response={"probabilities": probabilities},
        model="m",
    )
    assert await classification.record(conn, c)


async def test_a_miss_carries_what_each_stage_concluded(client, conn):
    from litwatch.core import Reaction, ReactionReason, ReactionValue
    from litwatch.feedback import misses

    # Reported before any stage ran: every cell pending.
    await misses.report(conn, Q.id, FAITH.slug)
    (m,) = (await client.get("/api/settings/misses")).json()
    assert (m["paper_id"], m["topic"], m["trace"]["classified"]) == (Q.id, FAITH.slug, False)
    assert m["trace"]["classifier"] is None and m["trace"]["graded"] is False
    # Classified under the Focus threshold, kept in by the band; graded by both labs.
    await _classify(conn, Q, {FAITH.slug: 0.3, "other": 0.9}, "other")
    await verdicts.add(
        conn,
        Verdict(
            paper_id=Q.id,
            topic=FAITH.slug,
            topic_version="v",
            fit=Fit.RELATED,
            reason="r",
            opinions={
                "judge": Opinion(fit=Fit.RELATED, reason="y", model="a"),
                "check": Opinion(fit=Fit.ADJACENT, reason="n", model="b"),
            },
        ),
    )
    (m,) = (await client.get("/api/settings/misses")).json()
    t = m["trace"]
    assert t["classified"] and t["probability"] == 0.3 and t["threshold"] == 0.5
    assert (t["classifier"], t["uncertain"], t["keyword"]) == (False, True, False)
    assert (t["graded"], t["judge"], t["check"]) == (True, "related", "adjacent")

    # Reactions over 30 days per topic and reason, for Overview.
    await word.react(
        conn,
        Reaction(
            paper_id=Q.id,
            topic=FAITH.slug,
            value=ReactionValue.DOWN,
            reason=ReactionReason.OVERRATED,
        ),
    )
    page = (await client.get("/api/settings/quality")).json()
    assert page["reactions"] == [{"topic": FAITH.slug, "reason": "overrated", "n": 1}]


async def test_taxonomy_stats_and_export(client, conn):
    from datetime import UTC, datetime

    recent = P.model_copy(update={"published": datetime.now(UTC)})
    await papers_store.upsert(conn, [recent])  # the keyword window is the last 90 days
    await _classify(conn, recent, {FAITH.slug: 0.9}, FAITH.slug)
    stats = (await client.get("/api/settings/taxonomy/stats")).json()
    # A member of the Focus class stands in its parent too.
    assert stats["members"] == {FAITH.slug: 1, "interpretability": 1}
    assert stats["topic_revisions"][FAITH.slug] == 1 and stats["class_revisions"]["other"] == 1
    kw = await client.get(f"/api/settings/topics/{FAITH.slug}/keywords")
    assert kw.json() == {r"chain.of.thought": 1, r"\bCoT\b": 0, "faithful": 1}
    assert (await client.get("/api/settings/topics/other/keywords")).status_code == 404
    text = (await client.get("/api/settings/taxonomy.yaml")).text
    assert text.startswith("classes:") and f"class: {FAITH.slug}" in text
