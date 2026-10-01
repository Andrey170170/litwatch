from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from litwatch.arxiv_source import window_query
from litwatch.ingest import run, window


def test_window_query_covers_categories_and_dates():
    since = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    until = datetime(2026, 9, 3, 12, 30, tzinfo=UTC)
    q = window_query(["cs.CL", "cs.AI"], since, until)
    assert q == "(cat:cs.CL OR cat:cs.AI) AND submittedDate:[202609010000 TO 202609031230]"


def test_window_ends_now_and_spans_days():
    now = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    since, until = window(3, now)
    assert until == now and (until - since).days == 3


async def test_busy_lock_still_closes_the_runs_archive_clients():
    class Busy:
        async def execute(self, sql):
            assert "pg_try_advisory_lock" in sql  # never unlocks a lock it did not take
            return SimpleNamespace(fetchone=AsyncMock(return_value={"locked": False}))

    routing, fetching = AsyncMock(), AsyncMock()
    router = SimpleNamespace(archive=routing)
    deep = SimpleNamespace(fetcher=SimpleNamespace(archive=fetching))
    with pytest.raises(RuntimeError, match="already in progress"):
        await run(Busy(), None, None, "daily", 1, router=router, deep=deep)
    routing.aclose.assert_awaited_once()
    fetching.aclose.assert_awaited_once()
