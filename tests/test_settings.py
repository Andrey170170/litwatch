"""Settings: editing topics, Quality, the schedule and configuration."""

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import Grade, Opinion, Verdict
from litwatch.feedback import word
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts
from tests.conftest import FAITH, TOPICS, paper

TOKEN = "s3cret"
P = paper("2610.00001", title="Faithful traces", abstract="Chain-of-thought omits steps.")
Q = paper("2610.00002", title="Protein folding", abstract="Structures.")


@pytest.fixture
async def client(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [P, Q])
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, Config())
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


async def test_the_owners_order(client):
    order = ["agents", "interpretability", FAITH.slug]
    assert (
        await client.put("/api/settings/topics-order", json={"slugs": order})
    ).status_code == 204
    topics = (await client.get("/api/settings/topics")).json()
    assert [t["slug"] for t in topics if t["kind"] == "watch"] == [FAITH.slug]
    assert {t["slug"]: t["position"] for t in topics}["agents"] == 1


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


async def test_bad_input_is_refused_not_crashed(client):
    body = FAITH.model_dump(mode="json") | {"keywords": ["["]}
    assert (await client.put(f"/api/settings/topics/{FAITH.slug}", json=body)).status_code == 422
    order = {"slugs": ["agents", "agents", FAITH.slug]}
    assert (await client.put("/api/settings/topics-order", json=order)).status_code == 422


async def test_settings_refuse_what_does_not_exist(client):
    assert (await client.post("/api/settings/calibrate/999/more")).status_code == 404
