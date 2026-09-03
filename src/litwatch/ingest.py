"""One run: fetch the arXiv window, store, screen whatever is pending, record the run."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import arxiv_source, db
from litwatch.config import Settings
from litwatch.screen import Cascade

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    run_id: int
    kind: str
    window_from: datetime
    window_to: datetime
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    screened: int = 0
    rescreened: int = 0
    confirmed: int = 0
    failed: int = 0
    cost: float = 0.0
    tiers: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def window(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """[now - days, now]. Daily runs use a few days so announcement lag and outages overlap."""
    now = now or datetime.now(UTC)
    return now - timedelta(days=days), now


async def run(
    conn: psycopg.AsyncConnection,
    settings: Settings,
    cascade: Cascade,
    kind: str,
    days: int,
    fetch: bool = True,
) -> RunSummary:
    since, until = window(days)
    run_id = await db.start_run(conn, kind, since, until)
    summary = RunSummary(run_id=run_id, kind=kind, window_from=since, window_to=until)
    try:
        if fetch:
            await _fetch(conn, settings, summary)
        await _screen(conn, settings, cascade, summary)
    except Exception as exc:  # noqa: BLE001 - recorded on the run row, then re-raised
        summary.error = f"{type(exc).__name__}: {exc}"
        log.exception("run %s failed", run_id)
        raise
    finally:
        await db.finish_run(
            conn,
            run_id,
            fetched=summary.fetched,
            inserted=summary.inserted,
            updated=summary.updated,
            screened=summary.screened,
            rescreened=summary.rescreened,
            confirmed=summary.confirmed,
            failed=summary.failed,
            cost=round(summary.cost, 4),
            tiers=summary.tiers,
            error=summary.error,
        )
    return summary


async def _fetch(conn: psycopg.AsyncConnection, settings: Settings, s: RunSummary) -> None:
    # The arxiv client is synchronous and paces itself; run it off the event loop.
    papers = await asyncio.to_thread(
        lambda: list(
            arxiv_source.fetch_window(list(settings.categories), s.window_from, s.window_to)
        )
    )
    s.fetched = len(papers)
    s.inserted, s.updated = await db.upsert_papers(conn, papers)
    log.info("run %s fetched %s (%s new, %s updated)", s.run_id, s.fetched, s.inserted, s.updated)


async def _screen(
    conn: psycopg.AsyncConnection, settings: Settings, cascade: Cascade, s: RunSummary
) -> None:
    todo = await db.pending(conn)
    sem = asyncio.Semaphore(settings.screen_concurrency)
    lock = asyncio.Lock()  # one connection, so serialize the writes

    async def one(paper):
        async with sem:
            try:
                result = await cascade.screen(paper)
            except Exception as exc:  # noqa: BLE001 - per-paper failure, retried next run
                async with lock:
                    await db.store_screen_error(conn, paper.id, f"{type(exc).__name__}: {exc}")
                    s.failed += 1
                return
            async with lock:
                await db.store_screen(conn, result)
                s.screened += 1
                s.rescreened += int(result.rescreened)
                s.confirmed += int(result.confirmed)
                s.cost += result.cost
                s.tiers[result.tier.value] = s.tiers.get(result.tier.value, 0) + 1

    await asyncio.gather(*(one(p) for p in todo))
    log.info(
        "run %s screened %s (%s re-screened, %s confirmed, %s failed) cost $%.4f tiers %s",
        s.run_id,
        s.screened,
        s.rescreened,
        s.confirmed,
        s.failed,
        s.cost,
        s.tiers,
    )
