"""One run: fetch the arXiv window, store, screen the papers nothing has judged yet, route,
deep-process, and write the digest; record the run. Papers judged under an older selection
version are a backlog, drained in the background between runs."""

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import archive_outbox, arxiv_source, db, intake
from litwatch.calls import CallJournal, current_journal
from litwatch.config import Settings
from litwatch.deep import DeepProcessor
from litwatch.digest import Digest, Digester
from litwatch.digest_history import write_run_digests
from litwatch.models import Paper
from litwatch.route import Router
from litwatch.screen import Cascade
from litwatch.selection import Selection

log = logging.getLogger(__name__)

# One writer for checkpoints and screening, shared by the API, the CLI and the backlog drain.
RUN_LOCK = 7213902
# A drain lets go after the batch in hand; a run that waited this long has another cause.
RUN_LOCK_WAIT = "15min"


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
    # Running tier counts behind `tiers`.
    best: dict[str, int] = field(default_factory=dict)
    by_selection: dict[str, dict[str, int]] = field(default_factory=dict)
    error: str | None = None
    # Routing/deep failures that did not stop the run, so the digest was still written.
    stage_errors: str | None = None
    intake: dict[str, object] | None = None


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
    # API and CLI share this lock, including analysis: checkpoints and pending screens must
    # have one writer even if two entry points are triggered together. A backlog drain is not
    # another run: it gives way, so this run records itself and waits for the lock.
    # The run owns the archive clients either way, including when the lock is busy.
    try:
        run_id = await _lock(conn, kind, days)
        try:
            return await _run(
                conn, settings, cascade, kind, days, fetch, router, digester, deep, run_id
            )
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK,))
    finally:
        clients = [
            getattr(router, "archive", None),
            getattr(getattr(deep, "fetcher", None), "archive", None),
        ]
        for client in clients:
            if client is not None:
                await client.aclose()


async def _try_lock(conn: psycopg.AsyncConnection) -> bool:
    row = await (
        await conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (RUN_LOCK,))
    ).fetchone()
    return bool(row and row["locked"])


async def _lock(conn: psycopg.AsyncConnection, kind: str, days: int) -> int | None:
    """Take the run lock. Returns the id of a run row opened while waiting for a drain.

    The row is opened before the wait so a caller polling for its run sees it at once.
    """
    if await _try_lock(conn):
        return None
    draining = await (
        await conn.execute("SELECT 1 FROM runs WHERE kind = 'backlog' AND finished_at IS NULL")
    ).fetchone()
    if not draining:
        raise RuntimeError("another Litwatch run is already in progress")
    run_id = await db.start_run(conn, kind, *window(days))
    log.info("run %s waiting for the backlog drain to give way", run_id)
    try:
        await conn.execute(f"SET lock_timeout = '{RUN_LOCK_WAIT}'")
        await conn.execute("SELECT pg_advisory_lock(%s)", (RUN_LOCK,))
    except psycopg.errors.LockNotAvailable as exc:
        await db.finish_run(conn, run_id, error="the backlog drain did not give way")
        raise RuntimeError("another Litwatch run is already in progress") from exc
    finally:
        await conn.execute("SET lock_timeout = 0")
    return run_id


async def run_waiting(conn: psycopg.AsyncConnection) -> bool:
    """Whether some run is blocked on the run lock, which a drain must then release."""
    row = await (
        await conn.execute(
            """
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory' AND NOT granted AND classid = 0 AND objid = %s
              AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
            LIMIT 1
            """,
            (RUN_LOCK,),
        )
    ).fetchone()
    return row is not None


async def drain_backlog(
    conn: psycopg.AsyncConnection,
    settings: Settings,
    cascade: Cascade,
    router: Router | None,
    stop: asyncio.Event,
    failed: set[str] | None = None,
) -> RunSummary | None:
    """Re-screen papers judged under an older selection version, until none are left or a run
    wants the lock. Returns None when there was nothing to do or a run is in progress.

    Each batch is screened and then routed, so Zotero follows along and the wait a run sees
    is one batch. Nothing here is news: no intake, no deep processing, no digest. Papers whose
    screening fails join `failed` and are left alone by later drains that are handed the set.
    """
    failed = failed if failed is not None else set()
    if not await _try_lock(conn):
        return None
    try:
        todo = await db.backlog(
            conn, cascade.selections, settings.rescreen_window_days, frozenset(failed)
        )
        if not todo:
            return None
        now = datetime.now(UTC)
        s = RunSummary(await db.start_run(conn, "backlog", now, now), "backlog", now, now)
        log.info("run %s backlog: %s papers to re-screen", s.run_id, len(todo))
        journal = CallJournal(conn, s.run_id)
        token = current_journal.set(journal)
        stage_errors: list[str] = []
        try:
            while todo and not stop.is_set() and not await run_waiting(conn):
                batch, todo = todo[: settings.backlog_batch], todo[settings.backlog_batch :]
                failed |= await _screen(
                    conn, settings, cascade, s, batch, settings.backlog_concurrency, quiet=True
                )
                if router is not None:
                    await _isolated(stage_errors, "routing", route_pending(conn, router, s))
            log.info("run %s backlog: %s re-screened, %s left", s.run_id, s.screened, len(todo))
        except Exception as exc:  # noqa: BLE001 - recorded on the run row, then re-raised
            s.error = f"{type(exc).__name__}: {exc}"
            log.exception("run %s backlog failed", s.run_id)
            raise
        finally:
            current_journal.reset(token)
            s.cost = await journal.total()
            s.stage_errors = "; ".join(dict.fromkeys(stage_errors)) or None
            await _finish(conn, s)
        return s
    finally:
        await conn.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK,))


async def _run(
    conn: psycopg.AsyncConnection,
    settings: Settings,
    cascade: Cascade,
    kind: str,
    days: int,
    fetch: bool,
    router: Router | None,
    digester: Digester | None,
    deep: DeepProcessor | None,
    run_id: int | None = None,
) -> RunSummary:
    since, until = window(days)
    if run_id is None:
        run_id = await db.start_run(conn, kind, since, until)
    summary = RunSummary(run_id=run_id, kind=kind, window_from=since, window_to=until)
    journal = CallJournal(conn, run_id)
    journal_token = current_journal.set(journal)
    try:
        if fetch:
            await _fetch(conn, settings, summary)
        else:
            summary.intake = await intake.record_skipped(conn, run_id)
        await _screen(conn, settings, cascade, summary)
        # Routing and deep processing enrich the digest but must not cancel it: a failure
        # is recorded on the run, its papers are retried next run, and the issue is written.
        stage_errors: list[str] = []
        if router is not None:
            await _isolated(stage_errors, "routing", route_pending(conn, router, summary))
        # Deep before digest: tier 1 prose is written from the reading card.
        if deep is not None:
            await _isolated(
                stage_errors,
                "deep",
                process_deep(conn, deep, summary, settings.deep_max_per_run),
            )
        summary.stage_errors = "; ".join(stage_errors) or None
        if digester is not None:
            await write_run_digest(conn, digester, cascade.selections, summary)
    except Exception as exc:  # noqa: BLE001 - recorded on the run row, then re-raised
        summary.error = f"{type(exc).__name__}: {exc}"
        log.exception("run %s failed", run_id)
        raise
    finally:
        current_journal.reset(journal_token)
        # Costs belong to the run that made the request, including successful early
        # stages of failed papers and rejected digest responses. Reuse is free.
        summary.cost = await journal.total()
        await _finish(conn, summary)
    return summary


async def _finish(conn: psycopg.AsyncConnection, s: RunSummary) -> None:
    await db.finish_run(
        conn,
        s.run_id,
        fetched=s.fetched,
        inserted=s.inserted,
        updated=s.updated,
        screened=s.screened,
        rescreened=s.rescreened,
        confirmed=s.confirmed,
        failed=s.failed,
        routed=s.routed,
        route_failed=s.route_failed,
        processed=s.processed,
        process_failed=s.process_failed,
        deep_list_cost=round(s.deep_list_cost, 4),
        cost=round(s.cost, 4),
        tiers=s.tiers,
        error=s.error,
        stage_errors=s.stage_errors,
        intake=s.intake,
    )


async def _isolated(errors: list[str], name: str, stage: Awaitable[None]) -> None:
    """Await an enrichment stage; record its failure instead of ending the run."""
    try:
        await stage
    except Exception as exc:  # noqa: BLE001 - recorded on the run row as stage_errors
        log.exception("%s stage failed", name)
        errors.append(f"{name}: {type(exc).__name__}: {exc}")


async def _fetch(conn: psycopg.AsyncConnection, settings: Settings, s: RunSummary) -> None:
    policy = archive_outbox.ArchivePolicy(settings.categories) if settings.archive_enabled else None
    if settings.intake_source == "oai":
        result = await intake.collect(
            conn, settings.categories, s.window_from, s.run_id, archive_policy=policy
        )
        s.fetched, s.inserted, s.updated = result.fetched, result.inserted, result.updated
        s.intake = result.details
        log.info(
            "run %s intake %s: %s papers (%s new, %s updated)",
            s.run_id,
            s.intake["status"],
            s.fetched,
            s.inserted,
            s.updated,
        )
        return
    if settings.intake_source != "search":
        raise ValueError(f"Unknown intake source: {settings.intake_source}")
    # The arxiv client is synchronous and paces itself; run it off the event loop.
    papers = await asyncio.to_thread(
        lambda: list(
            arxiv_source.fetch_window(list(settings.categories), s.window_from, s.window_to)
        )
    )
    s.fetched = len(papers)
    s.inserted, s.updated = await db.upsert_papers(
        conn, papers, archive_policy=policy, archive_run_id=s.run_id
    )
    log.info("run %s fetched %s (%s new, %s updated)", s.run_id, s.fetched, s.inserted, s.updated)


async def _screen(
    conn: psycopg.AsyncConnection,
    settings: Settings,
    cascade: Cascade,
    s: RunSummary,
    todo: list[Paper] | None = None,
    concurrency: int | None = None,
    *,
    quiet: bool = False,
) -> set[str]:
    """Screen the papers nothing has judged yet, or the given backlog batch (`quiet`).

    Returns the ids whose screening failed."""
    if todo is None:
        todo = await db.pending(conn)
    log.info("run %s screening %s papers", s.run_id, len(todo))
    sem = asyncio.Semaphore(concurrency or settings.screen_concurrency)
    journal = current_journal.get()
    # Journal writes must not interleave with another paper's verdict transaction.
    lock = journal.lock if journal else asyncio.Lock()
    # A drain screens batch after batch into one summary, so the counts carry over.
    best: dict[str, int] = s.best
    by_selection: dict[str, dict[str, int]] = s.by_selection
    failed: set[str] = set()

    async def one(paper):
        async with sem:
            try:
                result = await cascade.screen(paper)
            except Exception as exc:  # noqa: BLE001 - per-paper failure, retried next run
                async with lock:
                    await db.store_screen_error(conn, paper.id, f"{type(exc).__name__}: {exc}")
                    s.failed += 1
                    failed.add(paper.id)
                return
            async with lock:
                try:
                    await db.store_screen(conn, result, quiet=quiet)
                except Exception as exc:  # noqa: BLE001 - a bad row must not end the run
                    await db.store_screen_error(conn, paper.id, f"{type(exc).__name__}: {exc}")
                    s.failed += 1
                    failed.add(paper.id)
                    return
                if settings.archive_enabled and result.best_tier == 1:
                    try:
                        async with conn.transaction():
                            await archive_outbox.promote(conn, paper.id)
                    except Exception:
                        log.exception("%s: archive priority promotion deferred", paper.id)
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
    if journal:
        s.cost = await journal.total()
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
    return failed


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
    """Write each stream's new coverage and add successful generation costs to the run."""
    digests = await write_run_digests(conn, digester, selections, s.run_id, store=True)
    s.cost += sum(d.cost or 0.0 for d in digests)
    log.info("run %s digests: %s", s.run_id, ", ".join(d.source for d in digests))
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
