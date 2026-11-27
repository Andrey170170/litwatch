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

from litwatch import analysis, classification, digests, fulltext, grading, intake, links
from litwatch.config import Config
from litwatch.core import FAILED_RUNS, Cause
from litwatch.core.fulltext import LEASE, RECONCILE_LOOKBACK
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate
from litwatch.integrations import archive as archive_integration
from litwatch.jobs.context import ModelRun, model_run
from litwatch.llm import ModelGate
from litwatch.llm.context import spending
from litwatch.store import analyses as analyses_store
from litwatch.store import classifications as classifications_store
from litwatch.store import digests as digests_store
from litwatch.store import fulltext as fulltext_store
from litwatch.store import jobs as jobs_store
from litwatch.store import runs
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts as verdicts_store

log = logging.getLogger(__name__)


async def classify(
    db: DatabaseGate,
    config: Config,
    run: ModelRun,
    limit: int | None = None,
    ids: list[str] | None = None,
) -> None:
    """Classify every paper whose current text has no classification, up to `limit`, of `ids`
    when given (a job's papers). A paper whose answers were unusable `FAILED_RUNS` runs in a row
    is left for the owner (awaiting classification)."""
    async with db.read("classify: due") as unit:
        tree = await taxonomy_store.load(unit)
        papers = (
            [] if tree is None else await classifications_store.due(unit, FAILED_RUNS, limit, ids)
        )
    if tree is None:
        log.warning("no taxonomy: nothing is classified until one is imported")
        return
    thresholds = config.classification.thresholds
    rules = word.rules(config)
    async with classification.classify(papers, tree, run.gateway, thresholds) as found:
        async for c in found:
            async with word.change(
                db, c.paper.id, cause=Cause.VERDICT, rules=rules, name="record classification"
            ) as done:
                recorded = await classification.record(done.unit, c)
            if recorded:
                run.add("classified" if c.error is None else "classification_failed")


async def analyse(
    db: DatabaseGate,
    config: Config,
    run: ModelRun,
    limit: int | None = None,
    ids: list[str] | None = None,
) -> None:
    """Analyse every paper whose current text is classified and not analysed, up to `limit`, of
    `ids` when given. A paper whose answers were unusable `FAILED_RUNS` runs in a row is left for
    the owner, who can have it tried again. A paper whose analysis says it releases code or
    weights has its links found, once per arXiv version (`links.due`)."""
    async with db.read("analyse: due") as unit:
        papers = await analyses_store.due(unit, FAILED_RUNS, limit, ids)
    rules = word.rules(config)
    async with analysis.analyse(papers, run.gateway) as found:
        async for a in found:
            async with word.change(
                db, a.paper.id, cause=Cause.VERDICT, rules=rules, name="record analysis"
            ) as done:
                recorded = await analysis.record(done.unit, a)
                if recorded and rules.find_links:
                    await links.due(done.unit, [a.paper.id])
            if recorded:
                run.add("analysed" if a.error is None else "analysis_failed")


async def grade(
    db: DatabaseGate,
    config: Config,
    run: ModelRun,
    limit: int | None = None,
    ids: list[str] | None = None,
) -> None:
    """Grade every paper whose current text is due a grading for some topic, up to `limit` papers,
    of `ids` when given (`grading.due`). A grading whose answers were unusable `FAILED_RUNS` runs in
    a row is left for the owner, who can have it graded now."""
    async with db.read("grade: due") as unit:
        tree = await taxonomy_store.load(unit)
        if tree is None:
            return
        due, passes = await grading.due(unit, tree, FAILED_RUNS, limit, ids)
    rules = word.rules(config)
    async with grading.grade(due, run.gateway, config.grading) as found:
        async for g in found:
            async with word.change(
                db, g.due.paper.id, cause=Cause.VERDICT, rules=rules, name="record grading"
            ) as done:
                recorded = await grading.record(done.unit, g)
            if recorded:
                run.add("graded" if g.verdict is not None else "grading_failed")
    async with db.write("grading passes") as unit:  # only once every grading is recorded
        await verdicts_store.passed(unit, passes)


async def text_passes(
    db: DatabaseGate,
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
    async with db.write("take back text passes", taxonomy="shared") as unit:
        run.add("fulltext_taken_back", await fulltext_store.take_back(unit, LEASE))
    async with db.read("reconcile text passes") as unit:
        reconciled = await fulltext_store.candidates(unit, RECONCILE_LOOKBACK)
    await word.settle_all(db, reconciled, cause=Cause.VERDICT, rules=word.rules(config))
    run.add("fulltext_reconciled", len(reconciled))
    async with db.write("retry failed text passes", taxonomy="shared") as unit:
        run.add("fulltext_retried", await fulltext_store.retry_failed(unit, FAILED_RUNS))
    archive = fulltext.archive_for(config)
    if archive is None:
        log.warning("no archive configured: full-text passes wait")
        return Counter()
    try:
        return await fulltext.work(db, config, archive, run=run, limit=limit, deadline=deadline)
    finally:
        await archive.aclose()


WAIT_TICK_SECONDS = 30.0  # how often the wait looks at the passes it waits for


async def _waiting(db: DatabaseGate, since: datetime) -> bool:
    async with db.read("digest wait: passes") as unit:
        return bool(await digests_store.waiting_passes(unit, since))


async def wait_for_texts(
    db: DatabaseGate,
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
        while await _waiting(db, since):
            left = deadline - time.monotonic()
            if left <= 0:
                run.add("digest_wait_ended", 1)  # some passes still running: digests went on
                return
            await asyncio.sleep(min(tick, left))
            if limit is None or limit > 0:
                done = await fulltext.work(
                    db, config, archive, run=run, limit=limit, deadline=deadline
                )
                limit = None if limit is None else limit - fulltext.worked(done)
    finally:
        await archive.aclose()


async def write_digests(db: DatabaseGate, config: Config, run: ModelRun, today: date) -> None:
    """The night's digests, those of `today`."""
    async with db.read("digests: taxonomy") as unit:
        tree = await taxonomy_store.load(unit)
    if tree is None:
        return
    await digests.nightly(db, run, tree, today, rules=word.rules(config))


async def run(
    db: DatabaseGate,
    config: Config,
    *,
    models: ModelGate,
    fetch: bool = True,
    days: int | None = None,
    limit: int | None = None,
    today: date | None = None,
    with_digests: bool = True,
    night: tuple[datetime, datetime] | None | Literal["schedule"] = "schedule",
) -> int:
    """One run (`locked`) under the run lock, taken here: `BusyError` when another job holds
    it. Returns the run id. Once it lets go of the lock, the chunks it held back go (`wake`);
    one that never took it held none back."""
    held = False
    try:
        async with db.held("run"):
            held = True
            return await locked(
                db,
                config,
                fetch=fetch,
                days=days,
                limit=limit,
                models=models,
                today=today,
                with_digests=with_digests,
                night=night,
            )
    finally:
        if held:
            await wake(db)


async def wake(db: DatabaseGate) -> None:
    """Bring forward the job chunks a nightly held back, its run lock let go of
    (`store.jobs.wake`). A failure is logged: they go at their next look."""
    try:
        async with db.write("nightly: wake the jobs") as unit:
            await jobs_store.wake(unit)
    except Exception:
        log.exception("nightly: waking the jobs it held back failed")


async def locked(
    db: DatabaseGate,
    config: Config,
    *,
    models: ModelGate,
    fetch: bool = True,
    days: int | None = None,
    limit: int | None = None,
    today: date | None = None,
    with_digests: bool = True,
    night: tuple[datetime, datetime] | None | Literal["schedule"] = "schedule",
) -> int:
    """One run, under the run lock its caller holds, its calls through `models`, the process's
    Model gate. Returns the run id.

    A run started between the schedule's nightly time and its deadline (`night`: its start
    and deadline, by default `Schedule.night` of now) runs its steps under that deadline
    (`llm.context`), so its calls on a provider that batches go in the gate's batches for it
    until the deadline, and it waits for those batches before it ends. Then its digests wait
    for the passes until the deadline, those queued since the night's first run started
    included (docs/specs/v1/models.md, batches). Outside the night's window it sends no batch,
    its deadline its own start: a dead run's batch it asks again is cancelled and waited for.

    An incomplete harvest does not stop the rest: what arrived is classified, analysed and
    graded, and the gap stays open for the next run. `limit` caps the papers each step that
    spends takes, and the full-text passes tried; the full-text reconcile spends nothing and
    takes all. The digests are those of `today`, by default the day the run started on in the
    schedule's zone, however long it runs. Without `with_digests` the run neither waits for
    the passes nor writes digests: a window's are written by `jobs.digests.window` (the
    restart), which today's would otherwise take the papers of. As the run closes, failed or
    cut off too, the jobs it held back go, those on tonight's run included: released in the
    unit that closes it, under the `tonight` lock (`store.jobs.release`).
    """
    until = datetime.now(UTC)
    since = until - timedelta(days=days or config.intake.window_days)
    batch_night = config.schedule.night(until) if night == "schedule" else night
    today = today or until.astimezone(config.schedule.zone).date()
    release = (jobs_store.release, ["tonight"])
    async with model_run(
        db, "nightly", window=(since, until), models=models, closing=release
    ) as run:
        if fetch:
            result = await intake.collect(
                db,
                config.intake.categories,
                since,
                run.run_id,
                archive=archive_integration.harvester(config),
                rules=word.rules(config),
            )
            async with db.write("nightly: intake") as unit:
                await runs.update(
                    unit,
                    run.run_id,
                    fetched=result.fetched,
                    inserted=result.inserted,
                    updated=result.updated,
                    intake=result.details,
                )
            run.counts["rewritten"] = len(result.rewritten)
        # Outside the night's window, its own start: past, so it sends no batch, and a dead
        # run's batch it asks again is cancelled at once and waited for (`batches.DRAIN`).
        with spending(deadline=batch_night[1] if batch_night else until):
            await _steps(db, config, run, until, today, limit, with_digests, batch_night)
            await run.gateway.settle()  # its batches, a dead run's it took over included
    return run.run_id


async def _steps(
    db: DatabaseGate,
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
        async with db.read("nightly: first run tonight") as unit:
            since = await runs.first_since(unit, "nightly", night[0]) or until
    batching = run.gateway.batching
    await classify(db, config, run, limit)
    await analyse(db, config, run, limit)
    if word.rules(config).find_links:  # the automatic finds that failed lately, tried again
        await links.retry(db)
    await grade(db, config, run, limit)
    # The digests wait for the passes they depend on: until the night's deadline; from now and
    # at most `wait_minutes` outside the night, or when nothing batches (the passes go as
    # normal calls), though never past the night's deadline.
    wait = config.digests.wait_minutes * 60
    if night:
        left = max(0.0, (night[1] - datetime.now(UTC)).total_seconds())
        wait = left if batching else min(left, wait)
    deadline = time.monotonic() + wait
    done = await text_passes(db, config, run, limit, deadline)
    if not with_digests:
        return
    left = None if limit is None else limit - fulltext.worked(done)
    await wait_for_texts(db, config, run, since, deadline, limit=left)
    await write_digests(db, config, run, today)
