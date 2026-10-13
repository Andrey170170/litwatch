"""The API's side of integrations: their cards under Settings, retry and skip, card requests,
the stored card on the paper page, and search providers merged into search."""

import threading
import time
from datetime import UTC, datetime

import httpx
import pytest

from litwatch import events, store
from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import SearchHit
from litwatch.integrations.webhook import Webhook
from litwatch.store import integrations as state
from litwatch.store import outbox
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from tests.conftest import TOPICS, deliver, paper, settled, start

TOKEN = "s3cret"
AUTH = {"authorization": f"Bearer {TOKEN}"}
P = paper("2610.00001", title="Hidden traces", abstract="Hidden computation in CoT.")


class Carder:
    """Answers card requests, and fails on the events it is told to."""

    name = "carder"
    description = "Makes cards on request."
    makes_cards = True

    def __init__(self):
        self.fail: set[int] = set()

    def wants(self, event):
        return event.type == events.CARD_REQUESTED

    async def handle(self, conn, event):
        if event.id in self.fail:
            raise RuntimeError("the card service is down")


class Shelf:
    """A search provider: some hits, after a delay, or an error."""

    description = "Answers searches."

    def __init__(self, name, hits=(), delay=0.0, error=None):
        self.name, self.hits, self.delay, self.error = name, list(hits), delay, error
        self.threads: list[str] = []

    def wants(self, event):
        return False

    async def handle(self, conn, event):
        pass

    def search(self, query, filters):
        self.threads.append(threading.current_thread().name)
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.hits


def hit(pid: str, day: int, score: float | None = None) -> SearchHit:
    return SearchHit(
        id=pid,
        title=f"Paper {pid}",
        authors=["A. Author"],
        published=datetime(2026, 9, day, tzinfo=UTC),
        score=score,
    )


@pytest.fixture
async def make(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [P])
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    clients = []

    async def make(integrations=(), config=None, timeout=None):
        app = create_app(dsn, TOKEN, config or Config(), integrations=list(integrations))
        if timeout is not None:
            app.state.search_timeout = timeout
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
        )
        clients.append(c)
        return c

    yield make
    for c in clients:
        await c.aclose()


async def test_settings_show_each_integration_with_its_lag_and_what_it_is_stuck_on(conn, make):
    carder = Carder()
    config = Config(integrations={"carder": {"use": "x.y"}, "old": {"enabled": False}})
    client = await make([carder, Shelf("shelf")], config)
    await start(conn, carder)
    first = await events.emit(conn, events.CARD_REQUESTED, P.id)
    await events.emit(conn, events.PAPER_SAVED, P.id)
    carder.fail = {first}
    await deliver(conn, carder)

    cards = {c["name"]: c for c in (await client.get("/api/settings/integrations")).json()}
    assert list(cards) == ["carder", "shelf", "old"]
    stuck = cards["carder"]
    assert stuck["enabled"] and stuck["description"] == "Makes cards on request."
    assert stuck["lag"] == 2 and stuck["answers_card_requests"]
    assert stuck["failing"]["event_id"] == first
    assert stuck["failing"]["type"] == "card.requested" and stuck["failing"]["failures"] == 1
    assert "the card service is down" in stuck["failing"]["error"]
    assert cards["shelf"]["search"] == {
        "queries": 0,
        "left_out": 0,
        "last_error": None,
        "last_error_at": None,
    }
    assert cards["old"]["enabled"] is False and cards["old"]["description"] is None

    # Retry: the backoff is waived. Skip: the cursor moves past it.
    assert (await client.post("/api/settings/integrations/carder/retry")).status_code == 204
    assert (await outbox.cursor(conn, "carder")).retry_at is None
    skipped = await client.post("/api/settings/integrations/carder/skip")
    assert skipped.json()["event_id"] == first
    assert (await client.post("/api/settings/integrations/carder/skip")).status_code == 409
    assert (await client.post("/api/settings/integrations/carder/retry")).status_code == 409
    assert (await client.post("/api/settings/integrations/nobody/retry")).status_code == 404
    await deliver(conn, carder)
    cards = {c["name"]: c for c in (await client.get("/api/settings/integrations")).json()}
    assert cards["carder"]["failing"] is None and cards["carder"]["skipped"] == 1


async def test_a_card_request_needs_an_integration_that_answers_it(conn, make):
    # Webhooks pass card requests on, even one filtered to them; neither makes a card.
    bare = await make(
        [Webhook("http://hook.test"), Webhook("http://h.test", name="cards", patterns=("card.*",))]
    )
    assert (await bare.post(f"/api/papers/{P.id}/card")).status_code == 409
    paper_page = (await bare.get(f"/api/papers/{P.id}")).json()
    assert paper_page["card"] is None and paper_page["card_requestable"] is False

    client = await make([Carder()])
    assert (await client.post("/api/papers/2610.99999/card")).status_code == 404
    assert (await client.get(f"/api/papers/{P.id}")).json()["card_requestable"] is True
    asked = await client.post(f"/api/papers/{P.id}/card")
    assert asked.status_code == 202
    event = await outbox.event(conn, asked.json()["event_id"])
    assert (event.type, event.paper_id) == (events.CARD_REQUESTED, P.id)

    # A stored card shows, integration or not, and is not asked for again.
    await state.put_card(conn, P.id, {"key_claim": "K."}, "deep-model", {}, [])
    assert (await client.post(f"/api/papers/{P.id}/card")).status_code == 409
    shown = (await bare.get(f"/api/papers/{P.id}")).json()
    assert shown["card"]["card"] == {"key_claim": "K."} and shown["card"]["model"] == "deep-model"
    assert shown["card_requestable"] is False


async def test_search_adds_what_providers_find_outside_the_collection(conn, make):
    archive = Shelf("archive", [hit("2609.00002", 2), hit(P.id, 3), hit("2609.00003", 9)])
    slow = Shelf("slow", [hit("2609.00004", 4)], delay=0.5)
    broken = Shelf("broken", error=RuntimeError("index is rebuilding"))
    client = await make([archive, slow, broken], timeout=0.1)

    found = (await client.get("/api/papers", params={"q": "hidden"})).json()
    assert [r["id"] for r in found["rows"]] == [P.id]
    # The collection's paper is not repeated; newest first; each says who found it.
    assert [(r["id"], r["source"]) for r in found["outside"]] == [
        ("2609.00003", "archive"),
        ("2609.00002", "archive"),
    ]
    assert found["left_out"] == ["slow", "broken"]
    # On search's own pool: a provider stuck in its thread holds none of the default pool's.
    assert archive.threads and all(t.startswith("search") for t in archive.threads)
    cards = {c["name"]: c for c in (await client.get("/api/settings/integrations")).json()}
    assert cards["slow"]["search"]["left_out"] == 1
    assert "no answer within" in cards["slow"]["search"]["last_error"]
    assert "index is rebuilding" in cards["broken"]["search"]["last_error"]

    # Topic and saved are the collection's: no outside band with them; `source` picks a band.
    for params in ({"topic": "cot-faithfulness"}, {"saved": "true"}, {"source": "collection"}):
        got = (await client.get("/api/papers", params={"q": "hidden", **params})).json()
        assert got["outside"] == [] and got["left_out"] == []
    only = (await client.get("/api/papers", params={"q": "hidden", "source": "outside"})).json()
    assert only["rows"] == [] and len(only["outside"]) == 2


async def test_best_match_orders_the_outside_band_by_score(make):
    scored = Shelf("archive", [hit("2609.00002", 2, 1.0), hit("2609.00003", 9, 0.5)])
    client = await make([scored])
    found = (await client.get("/api/papers", params={"q": "x", "sort": "best"})).json()
    assert [r["id"] for r in found["outside"]] == ["2609.00002", "2609.00003"]


async def test_the_changes_feed_reads_on_from_a_position_and_misses_no_late_commit(conn, make):
    client = await make()
    await settled(conn)
    first = (await client.get("/api/events")).json()
    assert [e["type"] for e in first["events"]][:1] == ["paper.added"]  # the fixture's paper
    other = await store.sibling(conn)
    try:
        async with other.transaction():
            early = await events.emit(other, events.PAPER_SAVED, P.id)  # not yet committed
            late = await events.emit(conn, events.PAPER_UNSAVED, P.id)
            held = (await client.get("/api/events", params={"after": first["next"]})).json()
            assert held == {"events": [], "next": first["next"]}
    finally:
        await other.close()
    await settled(conn)
    newer = (await client.get("/api/events", params={"after": first["next"]})).json()
    assert [e["id"] for e in newer["events"]] == [early, late]
    assert newer["next"] != first["next"]
    assert (await client.get("/api/events", params={"after": "12"})).status_code == 422
    assert (await client.get("/api/events", params={"limit": 0})).status_code == 422
