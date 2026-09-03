"""One run: fetch the arXiv window, store, screen whatever is pending, record the run."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import arxiv_source, db
from litwatch.config import Settings
from litwatch.digest import Digester
from litwatch.route import Router
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
    routed: int = 0
    route_failed: int = 0
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
    router: Router | None = None,
    digester: Digester | None = None,
) -> RunSummary:
    since, until = window(days)
    run_id = await db.start_run(conn, kind, since, until)
    summary = RunSummary(run_id=run_id, kind=kind, window_from=since, window_to=until)
    try:
        if fetch:
            await _fetch(conn, settings, summary)
        await _screen(conn, settings, cascade, summary)
        if router is not None:
            await route_pending(conn, router, summary)
        if digester is not None:
            await write_run_digest(conn, digester, summary)
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
            routed=summary.routed,
            route_failed=summary.route_failed,
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


async def route_pending(
    conn: psycopg.AsyncConnection,
    router: Router,
    s: RunSummary | None = None,
    paper_id: str | None = None,
) -> list[tuple[str, str]]:
    """Route every tier 1/2 paper that is new or changed tier. Sequential: Zotero is one library.

    Returns (paper_id, error) for failures; each is retried on the next run.
    """
    failures: list[tuple[str, str]] = []
    for paper, result in await db.routable(conn, paper_id):
        try:
            routed = await router.route(paper, result)
        except Exception as exc:  # noqa: BLE001 - per-paper failure, retried next run
            error = f"{type(exc).__name__}: {exc}"
            log.warning("routing %s failed: %s", paper.id, error)
            await db.store_route_error(conn, paper.id, error)
            failures.append((paper.id, error))
            if s:
                s.route_failed += 1
            continue
        await db.store_route(
            conn, paper.id, routed.tier.value, routed.item_key, routed.attachment_key
        )
        if s:
            s.routed += 1
    if s:
        log.info("run %s routed %s (%s failed)", s.run_id, s.routed, s.route_failed)
    return failures


async def write_run_digest(conn: psycopg.AsyncConnection, digester: Digester, s: RunSummary) -> str:
    """Digest of the papers this run promoted to tier 1/2; the model cost joins the run's."""
    positives = await db.run_positives(conn, s.run_id)
    text, usage = await digester.write(positives)
    if usage:
        s.cost += usage.cost or 0.0
    await db.store_digest(conn, s.run_id, text, usage.model if usage else None)
    log.info("run %s digest: %d papers, %d chars", s.run_id, len(positives), len(text))
    return text
