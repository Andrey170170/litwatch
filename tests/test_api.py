"""The labelling API: the token, the queues, and what a label given there records."""

import httpx
import pytest

from litwatch.api import create_app
from litwatch.core import Label, LabelOrigin, LabelValue
from litwatch.store import feedback as feedback_store
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from tests.conftest import FAITH, TOPICS, paper

TOKEN = "s3cret"


READ_TOKEN = "r3ad"


@pytest.fixture
async def client(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [paper("2609.1"), paper("2609.2"), paper("2609.3")])
    # The app opens its own connections; point them at the test schema.
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    transport = httpx.ASGITransport(app=create_app(dsn, TOKEN, read_token=READ_TOKEN))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


AUTH = {"authorization": f"Bearer {TOKEN}"}
READ = {"authorization": f"Bearer {READ_TOKEN}"}


async def test_the_read_token_only_searches_and_reads_papers(client):
    found = await client.get("/api/papers", params={"q": "canonical", "limit": 5}, headers=READ)
    assert found.status_code == 200 and len(found.json()["rows"]) == 3
    assert (await client.get("/api/papers/2609.1", headers=READ)).json()["id"] == "2609.1"
    for method, path in [
        ("GET", "/api/status"),
        ("GET", "/api/labelling"),
        ("GET", "/api/settings/config"),
        ("PUT", "/api/library/2609.1"),
        ("POST", "/api/papers/2609.1/card"),
        ("PUT", "/api/papers/2609.1/publication"),
    ]:
        answer = await client.request(method, path, headers=READ, json={})
        assert answer.status_code == 403, (method, path)
    # Not a way in for the web app: no session and no cookie.
    assert (await client.post("/api/session", json={"token": READ_TOKEN})).status_code == 401
    client.cookies.set("litwatch_token", READ_TOKEN)
    assert (await client.get("/api/papers")).status_code == 401


async def test_everything_but_the_health_check_needs_the_token(client):
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/api/labelling")).status_code == 401
    assert (
        await client.get("/api/labelling", headers={"authorization": "Bearer no"})
    ).status_code == 401
    # Opening the page once with the token sets a cookie that then serves as the token.
    page = await client.get("/label", params={"token": TOKEN})
    assert "litwatch_token" in page.headers["set-cookie"]
    # It sends the browser on to the bare address, so the token does not stay in the URL.
    assert (page.status_code, page.headers["location"]) == (303, "/label")
    assert (await client.get("/api/labelling")).json()["topics"] == [
        {"slug": FAITH.slug, "name": FAITH.name}
    ]
    assert "set-cookie" not in (await client.get("/label", params={"token": "wrong"})).headers


async def test_a_label_records_how_its_paper_was_chosen_and_can_be_taken_back(client, conn):
    await feedback_store.add_provisional(
        conn,
        Label(
            paper_id="2609.1",
            topic=FAITH.slug,
            value=LabelValue.RELEVANT,
            origin=LabelOrigin.LEGACY_POSITIVE,
            note="0.14 tier 2",
        ),
    )
    sets = {s["key"]: s for s in (await client.get("/api/labelling", headers=AUTH)).json()["sets"]}
    assert (sets["positives"]["total"], sets["positives"]["done"]) == (1, 0)
    (item,) = (await client.get("/api/labelling/positives", headers=AUTH)).json()
    assert item["labels"][FAITH.slug]["provisional"] is True

    url = f"/api/labels/{FAITH.slug}/2609.1"
    body = {"value": "not_relevant", "queue": "positives"}
    assert (await client.put(url, json=body, headers=AUTH)).json()["provisional"] is False
    (label,) = await feedback_store.labels(conn)
    assert (label.value, label.origin) == (LabelValue.NOT_RELEVANT, LabelOrigin.LEGACY_POSITIVE)

    other = {"value": "unsure", "queue": "rejects"}
    await client.put(f"/api/labels/{FAITH.slug}/2609.2", json=other, headers=AUTH)
    assert (await feedback_store.labels(conn))[1].origin == LabelOrigin.REJECT_SAMPLE
    (unsure,) = (await client.get("/api/labelling/unsure", headers=AUTH)).json()
    assert unsure["id"] == "2609.2"
    assert (
        await client.delete(f"/api/labels/{FAITH.slug}/2609.2", headers=AUTH)
    ).status_code == 204
    assert len(await feedback_store.labels(conn)) == 1

    assert (
        await client.put("/api/labels/interpretability/2609.1", json=body, headers=AUTH)
    ).status_code == 404
    assert (
        await client.put(f"/api/labels/{FAITH.slug}/nope", json=body, headers=AUTH)
    ).status_code == 404
    assert (await client.get("/api/labelling/other", headers=AUTH)).status_code == 404


async def test_an_old_style_arxiv_id_with_a_slash_can_be_labelled(client, conn):
    await papers_store.upsert(conn, [paper("cs/9901001")])
    url = f"/api/labels/{FAITH.slug}/cs/9901001"
    assert (await client.put(url, json={"value": "relevant"}, headers=AUTH)).status_code == 200
    assert [label.paper_id for label in await feedback_store.labels(conn)] == ["cs/9901001"]
    assert (await client.delete(url, headers=AUTH)).status_code == 204
    assert await feedback_store.labels(conn) == []


async def test_a_good_bearer_header_works_beside_a_stale_cookie(client):
    client.cookies.set("litwatch_token", "old")
    assert (await client.get("/api/labelling", headers=AUTH)).status_code == 200
    # The scheme is checked, not skipped over.
    wrong_scheme = {"authorization": f"Basic  {TOKEN}"}
    assert (await client.get("/api/labelling", headers=wrong_scheme)).status_code == 401
