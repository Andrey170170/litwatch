"""The API's gate (the token and the read token) and its serving of the built web app."""

import httpx
import pytest

from litwatch.api import app as app_module
from litwatch.api import create_app
from litwatch.store import feedback as feedback_store
from litwatch.store import papers as papers_store
from tests import settling
from tests.conftest import FAITH, TREE, AppClient, paper
from tests.sessions import Session, in_read, in_write

TOKEN = "s3cret"


READ_TOKEN = "r3ad"


@pytest.fixture
async def client(conn: Session):
    await settling.save_tree(conn, TREE)
    await in_write(conn, papers_store.upsert, [paper("2609.1"), paper("2609.2"), paper("2609.3")])
    # The app's gate opens its own connections; point them at the test schema.
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, read_token=READ_TOKEN)
    async with AppClient(app, base_url="http://test") as c:
        yield c


async def test_an_app_given_a_gate_leaves_opening_and_closing_it_to_its_giver(db):
    app = create_app("postgresql://unused", TOKEN, db=db)
    async with app.router.lifespan_context(app):
        pass
    async with db.read("after the app stopped"):  # `serve`'s monitor still reads through it
        pass


AUTH = {"authorization": f"Bearer {TOKEN}"}
READ = {"authorization": f"Bearer {READ_TOKEN}"}


async def test_the_read_token_only_searches_and_reads_papers(client):
    found = await client.get("/api/papers", params={"q": "canonical", "limit": 5}, headers=READ)
    assert found.status_code == 200 and len(found.json()["rows"]) == 3
    assert (await client.get("/api/papers/2609.1", headers=READ)).json()["id"] == "2609.1"
    for method, path in [
        ("GET", "/api/summary"),
        ("GET", "/api/topics"),
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
    assert (await client.get("/api/summary")).status_code == 401
    assert (
        await client.get("/api/summary", headers={"authorization": "Bearer no"})
    ).status_code == 401
    assert (await client.get("/api/summary", headers=AUTH)).status_code == 200


async def test_an_old_style_arxiv_id_with_a_slash_can_be_labelled(client, conn: Session):
    await in_write(conn, papers_store.upsert, [paper("cs/9901001")])
    url = f"/api/labels/{FAITH.slug}/cs/9901001"
    assert (await client.put(url, json={"value": "related"}, headers=AUTH)).status_code == 200
    assert [label.paper_id for label in await in_read(conn, feedback_store.labels)] == [
        "cs/9901001"
    ]
    assert (await client.delete(url, headers=AUTH)).status_code == 200
    assert await in_read(conn, feedback_store.labels) == []


async def test_a_good_bearer_header_works_beside_a_stale_cookie(client):
    client.cookies.set("litwatch_token", "old")
    assert (await client.get("/api/summary", headers=AUTH)).status_code == 200
    # The scheme is checked, not skipped over.
    wrong_scheme = {"authorization": f"Basic  {TOKEN}"}
    assert (await client.get("/api/summary", headers=wrong_scheme)).status_code == 401


def _web(app) -> httpx.AsyncClient:  # the web app's files: no database
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_the_built_web_app_is_served_with_its_index_for_client_routes(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<p>shell</p>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "manifest.webmanifest").write_text("{}")
    monkeypatch.setattr(app_module, "WEB", tmp_path)
    async with _web(create_app("postgresql://unused", TOKEN)) as web:
        assert (await web.get("/")).text == "<p>shell</p>"
        assert (await web.get("/papers?lookup=2609.1")).text == "<p>shell</p>"
        assert (await web.get("/assets/app.js")).text == "console.log(1)"
        assert (await web.get("/manifest.webmanifest")).text == "{}"
        # An unknown API path is not the app's: it stays a 404.
        assert (await web.get("/api/nothing-here", headers=AUTH)).status_code == 404


async def test_an_unbuilt_web_app_says_how_to_build_it(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "WEB", tmp_path)
    async with _web(create_app("postgresql://unused", TOKEN)) as web:
        assert "pnpm build" in (await web.get("/")).text
