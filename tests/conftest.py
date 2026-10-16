"""Shared fixtures: a migrated database in a throwaway schema, a few topics and papers, and an
arXiv that stays slow."""

import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import arxiv
import pytest

from litwatch import store
from litwatch.core import Depth, Paper, Taxonomy, TaxonomyClass, Topic
from litwatch.events import dispatch

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


async def settled(conn, timeout: float = 20.0) -> None:
    """Wait until every event in the outbox is settled. The test cluster is shared: another
    session's open transaction holds back delivery of every event written after it began."""
    query = (
        "SELECT NOT EXISTS (SELECT 1 FROM outbox "
        "WHERE xid >= pg_snapshot_xmin(pg_current_snapshot())) AS ok"
    )
    for _ in range(int(timeout / 0.05)):
        if (await (await conn.execute(query)).fetchone())["ok"]:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("events stayed unsettled: a transaction on the cluster is held open")


async def start(conn, integration) -> None:
    """`dispatch.start`, once what came before is settled, so it is all behind the cursor."""
    await settled(conn)
    await dispatch.start(conn, integration)


async def deliver(conn, integration, now=None) -> int:
    """`dispatch.deliver`, once the events written so far are settled."""
    await settled(conn)
    return await dispatch.deliver(conn, integration, now)


@pytest.fixture
def slow_arxiv(monkeypatch):
    """arXiv's API on a local socket: it answers 200, then trickles its body a byte every 50 ms
    and never finishes, so no socket read ever times out. Requests' real reads are what run."""
    stop = threading.Event()

    class Trickle(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/atom+xml")
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            try:
                while not stop.wait(0.05):
                    self.wfile.write(b" ")
            except OSError:
                pass  # the client hung up

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/api/query?{{}}"
    monkeypatch.setattr(arxiv.Client, "query_url_format", url)
    yield server
    stop.set()
    server.shutdown()
    server.server_close()
