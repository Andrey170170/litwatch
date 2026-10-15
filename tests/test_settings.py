"""Settings: the taxonomy and editing topics, Quality, the schedule and configuration."""

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import Grade, Opinion, Verdict
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
        conn, Verdict(paper_id=P.id, topic=FAITH.slug, topic_revision=2, grade=Grade.RELEVANT)
    )
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


async def test_quality_lists_last_nights_splits_and_config_hides_secrets(client, conn):
    await conn.execute("INSERT INTO runs (kind, finished_at) VALUES ('nightly', now())")
    for topic in (FAITH.slug, "interpretability"):  # an Area topic's split takes no label
        await verdicts.add(
            conn,
            Verdict(
                paper_id=P.id,
                topic=topic,
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
    body = _body(FAITH) | {"keywords": ["["]}
    assert (await client.put(f"/api/settings/topics/{FAITH.slug}", json=body)).status_code == 422
    order = {"slugs": ["agents", "agents", FAITH.slug]}
    assert (await client.put("/api/settings/topics-order", json=order)).status_code == 422


async def test_settings_refuse_what_does_not_exist(client):
    assert (await client.post("/api/settings/calibrate/999/more")).status_code == 404
