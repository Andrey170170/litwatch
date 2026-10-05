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
        async def execute(self, sql, params=None):
            if "FROM runs" in sql:  # no backlog drain holds the lock, so there is no waiting
                return SimpleNamespace(fetchone=AsyncMock(return_value=None))
            assert "pg_try_advisory_lock" in sql  # never unlocks a lock it did not take
            return SimpleNamespace(fetchone=AsyncMock(return_value={"locked": False}))

    routing, fetching = AsyncMock(), AsyncMock()
    router = SimpleNamespace(archive=routing)
    deep = SimpleNamespace(fetcher=SimpleNamespace(archive=fetching))
    with pytest.raises(RuntimeError, match="already in progress"):
        await run(Busy(), None, None, "daily", 1, router=router, deep=deep)
    routing.aclose.assert_awaited_once()
    fetching.aclose.assert_awaited_once()


async def test_failed_enrichment_stage_still_writes_the_digest(monkeypatch):
    from litwatch import ingest

    finished, digested = {}, []

    async def start_run(conn, kind, since, until):
        return 7

    async def finish_run(conn, run_id, **fields):
        finished.update(fields)

    async def fails(*args, **kwargs):
        raise RuntimeError("Zotero is down")

    async def noop(*args, **kwargs):
        return None

    async def digest(conn, digester, selections, summary):
        digested.append(summary.stage_errors)

    journal = SimpleNamespace(total=AsyncMock(return_value=0.0))
    monkeypatch.setattr(ingest.db, "start_run", start_run)
    monkeypatch.setattr(ingest.db, "finish_run", finish_run)
    monkeypatch.setattr(ingest, "CallJournal", lambda conn, run_id: journal)
    monkeypatch.setattr(ingest, "_fetch", noop)
    monkeypatch.setattr(ingest, "_screen", noop)
    monkeypatch.setattr(ingest, "route_pending", fails)
    monkeypatch.setattr(ingest, "process_deep", noop)
    monkeypatch.setattr(ingest, "write_run_digest", digest)
    cascade = SimpleNamespace(selections=[])
    summary = await ingest._run(
        None,
        SimpleNamespace(deep_max_per_run=10),
        cascade,
        "daily",
        1,
        True,
        router=object(),
        digester=object(),
        deep=object(),
    )
    assert digested == ["routing: RuntimeError: Zotero is down"]
    assert finished["error"] is None and finished["stage_errors"] == digested[0]
    assert summary.error is None
