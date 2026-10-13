"""Shared fixtures: a migrated database in a throwaway schema, and a few topics and papers."""

import asyncio
import os
from uuid import uuid4

import pytest

from litwatch import store
from litwatch.core import Paper, Topic, TopicKind
from litwatch.events import dispatch

FAITH = Topic(
    slug="cot-faithfulness",
    name="CoT faithfulness",
    kind=TopicKind.WATCH,
    subject="Whether a reasoning trace omits or contradicts a computation the model performed.",
    rubric="A method or benchmark for detecting such mismatch, meant to transfer across tasks.",
    keywords=[r"chain.of.thought", r"\bCoT\b", "faithful"],
)
INTERP = Topic(
    slug="interpretability",
    name="Interpretability",
    kind=TopicKind.RADAR,
    subject="Understanding the internal mechanisms and representations of neural networks.",
    interests="Methods that could verify what a reasoning trace claims.",
)
AGENTS = Topic(
    slug="agents",
    name="Agents",
    kind=TopicKind.PULSE,
    subject="Language models acting over multiple steps with tools and memory.",
)
TOPICS = [FAITH, INTERP, AGENTS]


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
