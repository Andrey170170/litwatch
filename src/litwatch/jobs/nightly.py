"""The nightly run: intake, classification, analysis, grading, the full-text queue and, after a
bounded wait for the passes they depend on, the digests, under the run lock and with a run
record. Each recorded Weight and verdict settles its paper (effective grades, rarity, the
library, its full-text pass) as it is recorded."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import psycopg

from litwatch import analysis, classification, digests, fulltext, grading, intake
from litwatch.config import Config
from litwatch.core import FAILED_RUNS, Cause
from litwatch.core.fulltext import LEASE, RECONCILE_LOOKBACK
from litwatch.feedback import word
from litwatch.integrations import archive as archive_integration
from litwatch.jobs.context import GatewayFactory, ModelRun, default_gateway, model_run
from litwatch.jobs.lock import run_lock
from litwatch.store import analyses as analyses_store
from litwatch.store import classifications as classifications_store
from litwatch.store import digests as digests_store
from litwatch.store import fulltext as fulltext_store
from litwatch.store import runs
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts as verdicts_store

log = logging.getLogger(__name__)


async def classify(
    conn: psycopg.AsyncConnection,
    config: Config,
    run: ModelRun,
    limit: int | None = None,
    ids: list[str] | None = None,
) -> None:
    """Classify every paper whose current text has no classification, up to `limit`, of `ids`
    when given (a job's papers). A paper whose answers were unusable `FAILED_RUNS` runs in a row
    is left for the owner (awaiting classification)."""
    tree = await taxonomy_store.load(conn)
    if tree is None:
        log.warning("no taxonomy: nothing is classified until one is imported")
        return
    papers = await classifications_store.due(conn, FAILED_RUNS, limit, ids)
    thresholds = config.classification.thresholds
    async with classification.classify(papers, tree, run.gateway, thresholds) as found:
        async for c in found:
            if await classification.record(conn, c):
                run.add("classified" if c.error is None else "classification_failed")


async def analyse(
    conn: psycopg.AsyncConnection,
    run: ModelRun,
    limit: int | None = None,
    ids: list[str] | None = None,
) -> None:
    """Analyse every paper whose current text is classified and not analysed, up to `limit`, of
    `ids` when given. A paper whose answers were unusable `FAILED_RUNS` runs in a row is left for
    the owner, who can have it tried again."""
    papers = await analyses_store.due(conn, FAILED_RUNS, limit, ids)
    async with analysis.analyse(papers, run.gateway) as found:
        async for a in found:
            if await analysis.record(conn, a):
                run.add("analysed" if a.error is None else "analysis_failed")


async def grade(
    conn: psycopg.AsyncConnection,
    config: Config,
    run: ModelRun,
    limit: int | None = None,
    ids: list[str] | None = None,
) -> None:
    """Grade every paper whose current text is due a grading for some topic, up to `limit` papers,
    of `ids` when given (`grading.due`). A grading whose answers were unusable `FAILED_RUNS` runs in
    a row is left for the owner, who can have it graded now."""
    tree = await taxonomy_store.load(conn)
    if tree is None:
        return
    due, passes = await grading.due(conn, tree, FAILED_RUNS, limit, ids)
    async with grading.grade(due, run.gateway, config.grading) as found:
        async for g in found:
            if await grading.record(conn, g):
                run.add("graded" if g.verdict is not None else "grading_failed")
    await verdicts_store.passed(conn, passes)  # only once every grading is recorded


async def text_passes(
    conn: psycopg.AsyncConnection,
    config: Config,
    run: ModelRun,
    limit: int | None = None,
    deadline: float | None = None,
) -> Counter[str]:
    """Reconcile the full-text queue with the database: settle every candidate paper
    (`store.fulltext.candidates`: those with a pass open, those the rule may give one, those
    read for an older version or revision), so a trigger missed costs at most a night
    (pipeline.md, sections 6 and 8); all of them, as `limit` caps what is spent. Then make
    the failed passes ready again, those with fewer than `FAILED_RUNS` failed runs; and try
    every pass that is due once, up to `limit` and until `deadline`: the passes this run queued
    have their text asked for now, and the service's worker runs each once its text is there.
    Returns how the tries ended."""
    # Only one nightly runs at a time, so a lease longer than a worker's is a dead run's.
    run.add("fulltext_taken_back", await fulltext_store.take_back(conn, LEASE))
    reconciled = await fulltext_store.candidates(conn, RECONCILE_LOOKBACK)
    await word.settle_all(conn, reconciled, cause=Cause.VERDICT)
    run.add("fulltext_reconciled", len(reconciled))
    run.add("fulltext_retried", await fulltext_store.retry_failed(conn, FAILED_RUNS))
    archive = fulltext.archive_for(config)
    if archive is None:
        log.warning("no archive configured: full-text passes wait")
        return Counter()
    try:
        return await fulltext.work(conn, config, archive, run=run, limit=limit, deadline=deadline)
    finally:
        await archive.aclose()


WAIT_TICK_SECONDS = 30.0  # how often the wait looks at the passes it waits for


async def wait_for_texts(
    conn: psycopg.AsyncConnection,
    config: Config,
    run: ModelRun,
    since: datetime,
    deadline: float,
    tick: float = WAIT_TICK_SECONDS,
    limit: int | None = None,
) -> None:
    """The bounded wait before the digests (digests.md, timing): until every Brief and Weight
    re-check the run queued (since `since`) has finished, failed in this run or is terminal,
    or until `deadline` (`time.monotonic()`: `[digests] wait_minutes` after grading ended),
    whichever comes first, working the queue meanwhile so a pass runs as soon as its text
    arrives. No batch of passes starts after the deadline, so the wait ends at most one batch
    late, and the passes worked, written or failed, stop at `limit`; past it the wait leaves them
    to the service's worker. Without an archive no pass can run, so there is no wait."""
    archive = fulltext.archive_for(config)
    if archive is None:
        return
    try:
        while await digests_store.waiting_passes(conn, since):
            left = deadline - time.monotonic()
            if left <= 0:
                run.add("digest_wait_ended", 1)  # some passes still running: digests went on
                return
            await asyncio.sleep(min(tick, left))
            if limit is None or limit > 0:
                done = await fulltext.work(
                    conn, config, archive, run=run, limit=limit, deadline=deadline
                )
                limit = None if limit is None else limit - fulltext.worked(done)
    finally:
        await archive.aclose()


async def write_digests(conn: psycopg.AsyncConnection, run: ModelRun, today: date) -> None:
    """The night's digests, those of `today`."""
    tree = await taxonomy_store.load(conn)
    if tree is None:
        return
    await digests.nightly(conn, run, tree, today)


async def run(
    conn: psycopg.AsyncConnection,
    config: Config,
    *,
    fetch: bool = True,
    days: int | None = None,
    limit: int | None = None,
    gateway: GatewayFactory = default_gateway,
    today: date | None = None,
    with_digests: bool = True,
    night: tuple[datetime, datetime] | None | Literal["schedule"] = "schedule",
) -> int:
    """One run under the run lock. Returns the run id.

    A run started between the schedule's nightly time and its deadline (`night`: its start
    and deadline, by default `Schedule.night` of now) sends its calls on a provider that
    batches in batches until the deadline. Then its digests wait for the passes until the
    deadline, those queued since the night's first run started included (docs/specs/v1/
    models.md, batches).

    An incomplete harvest does not stop the rest: what arrived is classified, analysed and
    graded, and the gap stays open for the next run. `limit` caps the papers each step that
    spends takes, and the full-text passes tried; the full-text reconcile spends nothing and
    takes all. The digests are those of `today`, by default the day the run started on in the
    schedule's zone, however long it runs. Without `with_digests` the run neither waits for
    the passes nor writes digests: a window's are written by `jobs.digests.window` (the
    restart), which today's would otherwise take the papers of.
    """
    until = datetime.now(UTC)
    since = until - timedelta(days=days or config.intake.window_days)
    batch_night = config.schedule.night(until) if night == "schedule" else night
    today = today or until.astimezone(config.schedule.zone).date()
    async with (
        run_lock(conn),
        model_run(conn, config, "nightly", window=(since, until), gateway=gateway) as run,
    ):
        if fetch:
            archive = archive_integration.harvester(config)
            try:
                result = await intake.collect(
                    conn, config.intake.categories, since, run.run_id, archive=archive
                )
            finally:
                if archive is not None:
                    await archive.aclose()
            await runs.update(
                conn,
                run.run_id,
                fetched=result.fetched,
                inserted=result.inserted,
                updated=result.updated,
                intake=result.details,
            )
            run.counts["rewritten"] = len(result.rewritten)
        try:
            await _steps(conn, config, run, until, today, limit, with_digests, batch_night)
            await run.gateway.settle()  # the batches a dead run left are accounted for too
        finally:
            await run.gateway.close()
        return run.run_id


async def _steps(
    conn: psycopg.AsyncConnection,
    config: Config,
    run: ModelRun,
    until: datetime,
    today: date,
    limit: int | None,
    with_digests: bool,
    night: tuple[datetime, datetime] | None,
) -> None:
    since = until
    if night:  # a run started in the night's window waits for the passes since its first run
        since = await runs.first_since(conn, "nightly", night[0]) or until
    # Outside the night's window the deadline is now: no batch is sent, and those a dead run
    # left open are cancelled and settled as at a deadline.
    run.gateway.batch_until(night[1] if night else datetime.now(UTC))
    await run.gateway.recover()
    batching = run.gateway.batching
    await classify(conn, config, run, limit)
    await analyse(conn, run, limit)
    await grade(conn, config, run, limit)
    # The digests wait for the passes they depend on: until the night's deadline; from now and
    # at most `wait_minutes` outside the night, or when nothing batches (the passes go as
    # normal calls), though never past the night's deadline.
    wait = config.digests.wait_minutes * 60
    if night:
        left = max(0.0, (night[1] - datetime.now(UTC)).total_seconds())
        wait = left if batching else min(left, wait)
    deadline = time.monotonic() + wait
    done = await text_passes(conn, config, run, limit, deadline)
    if not with_digests:
        return
    left = None if limit is None else limit - fulltext.worked(done)
    await wait_for_texts(conn, config, run, since, deadline, limit=left)
    await write_digests(conn, run, today)
