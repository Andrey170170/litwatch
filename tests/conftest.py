"""Shared fixtures: a migrated database in a throwaway schema, a few topics and papers, and an
arXiv that stays slow."""

import asyncio
import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

import arxiv
import httpx
import pytest

from litwatch import store
from litwatch.core import Depth, Paper, Taxonomy, TaxonomyClass, Topic
from litwatch.events import dispatch
from litwatch.feedback import word
from litwatch.gates.database import STRICT_ENV, DatabaseGate, identity_of
from litwatch.store import runs
from tests.settling import RULES


@pytest.fixture(autouse=True)
def automatic_deep(monkeypatch):
    """Every configuration settles by `RULES`: automatic Deep cards queued, as with the `deep`
    integration configured. A test of them off passes its own (`settling.OFF`); a test of how
    the configuration gives them reads `settling.configured`."""
    monkeypatch.setattr(word, "rules", lambda config: RULES)


@pytest.fixture(autouse=True)
def strict_gates(monkeypatch):
    """A unit opened inside a unit, or a gate called inside one, raises (`gates.database`); a
    test of the lenient way, as production runs, removes the variable."""
    monkeypatch.setenv(STRICT_ENV, "1")


# A small tree with one topic at each depth: Focus under its Area, a Field beside them.
FAITH = Topic(
    slug="cot-faithfulness",
    depth=Depth.FOCUS,
    monogram="CF",
    colour="#c026d3",
    subject="Whether a reasoning trace omits or contradicts a computation the model performed.",
    rubric="A method or benchmark for detecting such mismatch, meant to transfer across tasks.",
    keywords=[r"chain.of.thought", r"\bCoT\b", "faithful"],
)
INTERP = Topic(
    slug="interpretability",
    depth=Depth.AREA,
    monogram="IN",
    colour="#4f46e5",
    subject="Understanding the internal mechanisms and representations of neural networks.",
    interests="Methods that could verify what a reasoning trace claims.",
)
AGENTS = Topic(
    slug="agents",
    depth=Depth.FIELD,
    monogram="AG",
    colour="#ca8a04",
    subject="Language models acting over multiple steps with tools and memory.",
)
TOPICS = [FAITH, INTERP, AGENTS]
TREE = Taxonomy(
    classes=[
        TaxonomyClass(slug="interpretability", name="Interpretability", description=INTERP.subject),
        TaxonomyClass(
            slug="cot-faithfulness",
            name="Chain-of-thought faithfulness",
            description=FAITH.subject,
            parent="interpretability",
        ),
        TaxonomyClass(slug="agents", name="Agents and tool use", description=AGENTS.subject),
        TaxonomyClass(slug="other", name="Other", description="Papers that fit nothing above."),
    ],
    topics=TOPICS,
)


# The Deep read's fixtures, the web app's own (`deep/src/read.ts` is their schema).
READS = Path(__file__).parent.parent / "web" / "src" / "deepread" / "fixtures"
# Two WebP images, 8 by 6 pixels, of different colours.
BLUE = base64.b64decode("UklGRiIAAABXRUJQVlA4TBYAAAAvB0ABAAdQt6IUuf8BgCD8b5uI6H8I")
RUST = base64.b64decode("UklGRiIAAABXRUJQVlA4TBYAAAAvB0ABAAdQrSIXpf8BgCD8b5uI6H8I")


def fixture_read(name: str) -> dict[str, Any]:
    """One of the web app's fixture reads: `icl` or `scarce` (which carries a process note)."""
    return json.loads((READS / f"read-{name}.json").read_text())


def sent_figure(name: str, image: bytes = BLUE, mime: str = "image/webp") -> dict[str, str]:
    """A figure as the deep service sends it with a read (docs/integrations.md)."""
    return {"name": name, "mime": mime, "data": base64.b64encode(image).decode()}


def paper(id: str = "2609.11934", **fields) -> Paper:
    defaults = {
        "title": "Canonical title",
        "abstract": "Canonical abstract",
        "authors": ["Alice Smith", "Bob Jones"],
        "categories": ["cs.CL"],
        "primary_category": "cs.CL",
        "url": f"https://arxiv.org/abs/{id}",
    }
    return Paper(id=id, **{**defaults, **fields})


# The dev host's own services, which `.env.v1` exports for running litwatch: no test reaches one.
HOST_SERVICES = (
    "LITWATCH_ARCHIVE_URL",
    "LITWATCH_ARCHIVE_TOKEN",
    "LITWATCH_ARCHIVE_TOKEN_FILE",
    "LITWATCH_ZOTERO_USER_ID",
    "ZOTERO_API_KEY",
    "ZOTERO_API_KEY_FILE",
    "NOTION_TOKEN",
    "NOTION_TOKEN_FILE",
)


@pytest.fixture(autouse=True)
def no_host_services(monkeypatch):
    for name in HOST_SERVICES:
        monkeypatch.setenv(name, "")  # recorded, so the undo also removes what a test's .env sets
        monkeypatch.delenv(name)


@pytest.fixture
async def conn():
    """A connection to an empty, migrated schema. Skips without `TEST_DATABASE_URL`, which
    must name a disposable database, never a production one."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("needs a disposable TEST_DATABASE_URL")
    schema = "test_" + uuid4().hex
    async with await store.connect(dsn) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            async with await store.connect(dsn, schema) as c:
                await store.migrate(c)
                c.test_dsn, c.test_schema = dsn, schema
                yield c
        finally:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
async def db(conn):
    """The Database gate on `conn`'s schema, with a pool of its own."""
    async with gate_on(conn) as gate:
        yield gate


@pytest.fixture
async def gate(conn):
    """The Database gate on `conn`'s schema, as `db`, for the modules whose own `db` fixture is
    a seeded connection."""
    async with gate_on(conn) as g:
        yield g


def gate_on(conn) -> DatabaseGate:
    """A Database gate of its own on `conn`'s schema, with its own presence session: as another
    process's. Opened and closed with `async with`."""
    return DatabaseGate(conn.test_dsn, conn.test_schema)


async def started(conn, kind: str, *args: Any, **kwargs: Any) -> int:
    """`store.runs.start` in the name of `conn`'s own session, alive while `conn` is open."""
    return await runs.start(conn, kind, *args, me=await identity_of(conn), **kwargs)


class AppClient(httpx.AsyncClient):
    """An HTTP client on an app from `create_app`, which holds the app's Database gate open
    while the client is open, as the app's lifespan does, without the loops the lifespan
    starts: from entering it, or its first request, to leaving it or `aclose`. For the app's
    monitor, `app` is the monitor and `of` the app it reads."""

    def __init__(self, app: Any, *, of: Any = None, **kwargs: Any) -> None:
        super().__init__(transport=httpx.ASGITransport(app=app), **kwargs)
        self.gate, self._opened = (of or app).state.db, False

    async def _open(self) -> None:
        if not self._opened:
            await self.gate.open()
            self._opened = True

    async def __aenter__(self) -> Self:
        await self._open()
        return await super().__aenter__()

    async def send(self, *args: Any, **kwargs: Any) -> httpx.Response:
        await self._open()
        return await super().send(*args, **kwargs)

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await super().__aexit__(*exc)
        finally:
            await self.gate.close()

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            await self.gate.close()


async def settled(on, timeout: float = 20.0) -> None:
    """Wait until every event in the outbox is settled, read on a connection or a gate. The test
    cluster is shared: another session's open transaction holds back delivery of every event
    written after it began."""
    query = (
        "SELECT NOT EXISTS (SELECT 1 FROM outbox "
        "WHERE xid >= pg_snapshot_xmin(pg_current_snapshot())) AS ok"
    )

    async def ok() -> bool:
        if isinstance(on, DatabaseGate):
            async with on.read("settled") as unit:  # a fresh snapshot each time
                return (await (await unit.execute(query)).fetchone())["ok"]
        return (await (await on.execute(query)).fetchone())["ok"]

    for _ in range(int(timeout / 0.05)):
        if await ok():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("events stayed unsettled: a transaction on the cluster is held open")


async def start(db: DatabaseGate, integration) -> None:
    """`dispatch.start`, once what came before is settled, so it is all behind the cursor."""
    await settled(db)
    await dispatch.start(db, integration)


async def deliver(db: DatabaseGate, integration, now=None) -> int:
    """`dispatch.deliver`, once the events written so far are settled."""
    await settled(db)
    return await dispatch.deliver(db, integration, now)


def answering(answer):
    """A fake arXiv lookup (`search.fetch_by_ids`) that answers `answer(ids)` at once."""

    async def fetch_by_ids(ids):
        return answer(ids)

    return fetch_by_ids


@pytest.fixture
def slow_arxiv(monkeypatch):
    """arXiv's API on a local socket that never finishes answering: it trickles a byte every
    50 ms, so no socket read ever times out. With `trickle = "body"` (the default) it sends its
    status and headers, then the body; with `"headers"`, the headers themselves."""
    stop = threading.Event()

    class Trickle(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if server.trickle == "headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                    while not stop.wait(0.05):
                        self.wfile.write(b"X")
                        self.wfile.flush()
                self.send_response(200)
                self.send_header("Content-Type", "application/atom+xml")
                self.send_header("Content-Length", "1000000")
                self.end_headers()
                while not stop.wait(0.05):
                    self.wfile.write(b" ")
                    self.wfile.flush()
            except OSError:
                pass  # the client hung up

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    server.trickle = "body"
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/api/query?{{}}"
    monkeypatch.setattr(arxiv.Client, "query_url_format", url)
    yield server
    stop.set()
    server.shutdown()
    server.server_close()
