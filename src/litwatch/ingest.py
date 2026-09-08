"""One run: fetch the arXiv window, store, screen whatever is pending under the active
selections, route, deep-process, and write the digest; record the run."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import arxiv_source, db
from litwatch.config import Settings
from litwatch.deep import DeepProcessor
from litwatch.digest import Digest, Digester
from litwatch.route import Router
from litwatch.screen import Cascade
from litwatch.selection import Selection

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
    processed: int = 0
    process_failed: int = 0
    deep_list_cost: float = 0.0
    cost: float = 0.0
    tiers: dict[str, object] = field(default_factory=dict)
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
    deep: DeepProcessor | None = None,
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
        # Deep before digest: tier 1 prose is written from the reading card.
        if deep is not None:
            await process_deep(conn, deep, summary, settings.deep_max_per_run)
        if digester is not None:
            await write_run_digest(conn, digester, cascade.selections, summary)
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
            processed=summary.processed,
            process_failed=summary.process_failed,
            deep_list_cost=round(summary.deep_list_cost, 4),
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
    todo = await db.pending(conn, cascade.selections, settings.rescreen_window_days)
    log.info("run %s screening %s papers", s.run_id, len(todo))
    sem = asyncio.Semaphore(settings.screen_concurrency)
    lock = asyncio.Lock()  # one connection, so serialize the writes
    best: dict[str, int] = {}
    by_selection: dict[str, dict[str, int]] = {}

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
                key = str(result.best_tier)
                best[key] = best.get(key, 0) + 1
                for slug, v in result.verdicts.items():
                    if v.tier < 4:
                        counts = by_selection.setdefault(slug, {})
                        counts[str(v.tier)] = counts.get(str(v.tier), 0) + 1

    await asyncio.gather(*(one(p) for p in todo))
    s.tiers = {**best, "by_selection": by_selection}
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
    """Route every tier 1/2 paper that is new or changed tier, and release papers that dropped
    below tier 2. Sequential: Zotero is one library.

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
        await db.store_route(conn, paper.id, routed.tier, routed.item_key, routed.attachment_key)
        if s:
            s.routed += 1
    if paper_id is None:
        for row in await db.demoted(conn):
            try:
                await router.release(row["zotero_key"])
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                log.warning("releasing %s failed: %s", row["id"], error)
                await db.store_route_error(conn, row["id"], error)
                failures.append((row["id"], error))
                continue
            await db.store_route(conn, row["id"], "none", None, None)
            log.info("%s: released from Litwatch (tier %s)", row["id"], row["best_tier"])
    if s:
        log.info("run %s routed %s (%s failed)", s.run_id, s.routed, s.route_failed)
    return failures


async def write_run_digest(
    conn: psycopg.AsyncConnection,
    digester: Digester,
    selections: list[Selection],
    s: RunSummary,
) -> list[Digest]:
    """Digests of the papers this run placed in some selection; model costs join the run's."""
    members = await db.run_members(conn, s.run_id)
    digests = await digester.write_all(selections, members)
    for d in digests:
        s.cost += d.cost or 0.0
        await db.store_digest(conn, s.run_id, d.source, d.title, d.body, d.model, d.cost)
    log.info(
        "run %s digests: %d papers, %s",
        s.run_id,
        len(members),
        ", ".join(f"{d.source} {len(d.body)} chars" for d in digests),
    )
    return digests


async def process_deep(
    conn: psycopg.AsyncConnection, deep: DeepProcessor, s: RunSummary, limit: int
) -> None:
    """Deep-process pending tier 1 papers, at most `limit` per run."""
    result = await deep.process_pending(conn, limit=limit)
    s.processed, s.process_failed = result.processed, result.failed
    s.deep_list_cost = result.list_cost
    log.info(
        "run %s deep-processed %s (%s failed) list cost $%.2f",
        s.run_id,
        s.processed,
        s.process_failed,
        s.deep_list_cost,
    )
