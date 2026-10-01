"""Queue regression tests against a disposable PostgreSQL database.

Set LITWATCH_TEST_DATABASE_URL to an empty test DB; never a production database.
"""

import os
from unittest.mock import AsyncMock

import pytest
from psycopg.types.json import Jsonb

from litwatch import db, ingest
from litwatch.route import Routed
from tests.conftest import screen_result


@pytest.fixture
async def queue():
    dsn = os.environ.get("LITWATCH_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("requires disposable LITWATCH_TEST_DATABASE_URL")
    async with await db.connect(dsn) as conn:
        await db.apply_schema(conn)
        # All test writes go into a session-local table, including delivery receipts.
        await conn.execute("CREATE TEMP TABLE papers (LIKE public.papers INCLUDING ALL)")
        for i in range(207):
            tier = 1 if i >= 205 else 2
            result = screen_result(str(i), cot_faithfulness=tier)
            await conn.execute(
                """INSERT INTO papers (id, title, abstract, published, best_tier, screen)
                   VALUES (%s, 'Paper', 'Abstract', %s, %s, %s)""",
                (
                    str(i),
                    "2026-09-01" if tier == 1 else "2026-09-28",
                    tier,
                    Jsonb(result.model_dump(mode="json")),
                ),
            )
        yield conn


async def test_routes_entire_queue_prioritizes_tier1_and_retries_failures_once(queue):
    attempted = []

    async def deliver(paper, result):
        attempted.append(paper.id)
        if paper.id == "205":
            raise RuntimeError("temporary Zotero failure")
        return Routed(
            paper.id,
            result.best_tier,
            "item-" + paper.id,
            "pdf-" + paper.id if result.best_tier == 1 else None,
        )

    router = AsyncMock()
    router.route.side_effect = deliver
    failures = await ingest.route_pending(queue, router)
    assert len(attempted) == len(set(attempted)) == 207
    assert attempted[:2] == ["205", "206"]
    assert failures == [("205", "RuntimeError: temporary Zotero failure")]
    assert [p.id for p, _ in await db.routable(queue)] == ["205"]
    attempted.clear()
    router.route.side_effect = lambda p, r: Routed(p.id, r.best_tier, "item", "pdf")
    assert await ingest.route_pending(queue, router) == []
    assert await db.routable(queue) == []


async def test_explicit_limit_and_single_paper_still_work(queue):
    assert [p.id for p, _ in await db.routable(queue, limit=1)] == ["205"]
    assert [p.id for p, _ in await db.routable(queue, paper_id="3")] == ["3"]
