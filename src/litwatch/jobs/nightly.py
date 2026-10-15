"""The nightly run: intake, classification, analysis, grading and the full-text queue, under the
run lock and with a run record. Each recorded Weight and verdict settles its paper (effective
grades, rarity, the library, its full-text pass) as it is recorded."""

from __future__ import annotations

import logging
from contextlib import aclosing
from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import analysis, classification, fulltext, grading, intake
from litwatch.config import Config
from litwatch.core import FAILED_RUNS
from litwatch.feedback import word
from litwatch.jobs.context import GatewayFactory, ModelRun, default_gateway, model_run
from litwatch.jobs.lock import run_lock
from litwatch.store import analyses as analyses_store
from litwatch.store import classifications as classifications_store
from litwatch.store import fulltext as fulltext_store
from litwatch.store import runs
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts as verdicts_store

log = logging.getLogger(__name__)


async def classify(
    conn: psycopg.AsyncConnection, config: Config, run: ModelRun, limit: int | None = None
) -> None:
    """Classify every paper whose current text has no classification, up to `limit`. A paper
    whose answers were unusable `FAILED_RUNS` runs in a row is left for the owner (awaiting
    classification)."""
    tree = await taxonomy_store.load(conn)
    if tree is None:
        log.warning("no taxonomy: nothing is classified until one is imported")
        return
    papers = await classifications_store.due(conn, FAILED_RUNS, limit)
    thresholds = config.classification.thresholds
    async with aclosing(classification.classify(papers, tree, run.gateway, thresholds)) as found:
        async for c in found:
            if await classification.record(conn, c):
                run.add("classified" if c.error is None else "classification_failed")


async def analyse(conn: psycopg.AsyncConnection, run: ModelRun, limit: int | None = None) -> None:
    """Analyse every paper whose current text is classified and not analysed, up to `limit`.
    A paper whose answers were unusable `FAILED_RUNS` runs in a row is left for the owner, who
    can have it tried again."""
    papers = await analyses_store.due(conn, FAILED_RUNS, limit)
    async with aclosing(analysis.analyse(papers, run.gateway)) as found:
        async for a in found:
            if await analysis.record(conn, a):
                run.add("analysed" if a.error is None else "analysis_failed")


async def grade(
    conn: psycopg.AsyncConnection, config: Config, run: ModelRun, limit: int | None = None
) -> None:
    """Grade every paper whose current text is due a grading for some topic, up to `limit`
    papers (`grading.due`). A grading whose answers were unusable `FAILED_RUNS` runs in a row is
    left for the owner, who can have it graded now."""
    tree = await taxonomy_store.load(conn)
    if tree is None:
        return
    due, passes = await grading.due(conn, tree, FAILED_RUNS, limit)
    async with aclosing(grading.grade(due, run.gateway, config.grading)) as found:
        async for g in found:
            if await grading.record(conn, g):
                run.add("graded" if g.verdict is not None else "grading_failed")
    await verdicts_store.passed(conn, passes)  # only once every grading is recorded


async def text_passes(
    conn: psycopg.AsyncConnection, config: Config, run: ModelRun, limit: int | None = None
) -> None:
    """Settle the papers whose text was read, or its pass queued, for an older version or
    revision, which queues their passes again (pipeline.md, section 8); make the failed passes
    ready again, those with fewer than `FAILED_RUNS` failed runs; and try every pass that is due
    once, up to `limit`: the passes this run queued have their text asked for now, and the
    service's worker runs each once its text is there. Found in the database, not handed on by
    intake, so a run cut short leaves them to the next."""
    await word.settle_all(conn, await fulltext_store.outdated(conn))
    run.add("fulltext_retried", await fulltext_store.retry_failed(conn, FAILED_RUNS))
    archive = fulltext.archive_for(config)
    if archive is None:
        log.warning("no archive configured: full-text passes wait")
        return
    try:
        await fulltext.work(conn, config, archive, run=run, limit=limit)
    finally:
        await archive.aclose()


async def run(
    conn: psycopg.AsyncConnection,
    config: Config,
    *,
    fetch: bool = True,
    days: int | None = None,
    limit: int | None = None,
    gateway: GatewayFactory = default_gateway,
) -> int:
    """One run under the run lock. Returns the run id.

    An incomplete harvest does not stop the rest: what arrived is classified, analysed and
    graded, and the gap stays open for the next run. `limit` caps the papers each step takes,
    and the full-text passes tried.
    """
    until = datetime.now(UTC)
    since = until - timedelta(days=days or config.intake.window_days)
    async with (
        run_lock(conn),
        model_run(conn, config, "nightly", window=(since, until), gateway=gateway) as run,
    ):
        if fetch:
            result = await intake.collect(conn, config.intake.categories, since, run.run_id)
            await word.settle_all(conn, result.rewritten)  # the old text's Weight went with it
            await runs.update(
                conn,
                run.run_id,
                fetched=result.fetched,
                inserted=result.inserted,
                updated=result.updated,
                intake=result.details,
            )
            run.counts["rewritten"] = len(result.rewritten)
        await classify(conn, config, run, limit)
        await analyse(conn, run, limit)
        await grade(conn, config, run, limit)
        await text_passes(conn, config, run, limit)
        return run.run_id
