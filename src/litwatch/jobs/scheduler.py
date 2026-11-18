"""The in-process scheduler: the nightly run and the publication lookup at their cron times,
in the configured timezone. Off unless `[schedule] enabled = true`; nothing runs on a
schedule before cutover (spec decision 22).

It keeps no state of its own. A job is due when its cron expression fired after the job's
latest run on record, so a restart neither repeats a run nor forgets one. A fire missed by
more than `CATCH_UP` is skipped rather than run late. A job that finds the run lock taken
(another job is still going) stays due and is tried again on the next tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from croniter import croniter

from litwatch.config import Config
from litwatch.gates.database import BusyError, DatabaseGate, Hold
from litwatch.jobs import nightly, publications
from litwatch.jobs.context import GatewayFactory, default_gateway
from litwatch.publications import Dblp, PublicationSource
from litwatch.store import runs

log = logging.getLogger(__name__)

CATCH_UP = timedelta(hours=6)
TICK_SECONDS = 30


@dataclass(frozen=True)
class Job:
    kind: str  # the run kind it records, which is also how its last run is found
    cron: str
    # Run under the kind's schedule lock, which the scheduler holds (the `Hold`).
    run: Callable[[DatabaseGate, Hold], Awaitable[object]]


def last_fire(cron: str, now: datetime) -> datetime:
    """The most recent time at or before `now` (an aware time in the configured zone) the
    expression fired. The step past `now` is taken in UTC: wall-clock arithmetic would lose
    which of a fall-back's two hours `now` is in."""
    after = (now.astimezone(UTC) + timedelta(microseconds=1)).astimezone(now.tzinfo)
    return croniter(cron, after).get_prev(datetime)


def next_fire(cron: str, now: datetime) -> datetime:
    """The first time after `now` (an aware time in the configured zone) the expression
    fires, in that zone. The step past `now` is taken in UTC, as in `last_fire`, so the
    repeated hour of a fall-back is stepped through rather than skipped."""
    after = (now.astimezone(UTC) + timedelta(microseconds=1)).astimezone(now.tzinfo)
    return croniter(cron, after).get_next(datetime)


def is_due(cron: str, now: datetime, last_run: datetime | None) -> bool:
    fired = last_fire(cron, now).astimezone(UTC)
    if now.astimezone(UTC) - fired > CATCH_UP:
        return False
    return last_run is None or last_run.astimezone(UTC) < fired


def jobs(
    config: Config,
    gateway: GatewayFactory = default_gateway,
    source: PublicationSource | None = None,
) -> list[Job]:
    async def run_nightly(db: DatabaseGate, hold: Hold) -> object:
        return await nightly.run(db, config, gateway=gateway)

    async def run_publications(db: DatabaseGate, hold: Hold) -> object:
        # No model calls and no run lock: it only reads DBLP and writes publications.
        return await publications.run(db, source or Dblp(), hold=hold)

    return [
        Job("nightly", config.schedule.nightly, run_nightly),
        Job("publications", config.schedule.publications, run_publications),
    ]


async def _due(db: DatabaseGate, job: Job, now: datetime) -> bool:
    async with db.read(f"schedule: is {job.kind} due") as unit:
        return is_due(job.cron, now, await runs.last_started(unit, job.kind))


async def tick(db: DatabaseGate, todo: list[Job], now: datetime) -> list[str]:
    """Run every job that is due at `now`, in order, each under its kind's schedule lock, which
    it is given. Returns the kinds that ran."""
    ran = []
    for job in todo:
        if not await _due(db, job, now):
            continue  # asked again under the lock, which only a job that is due takes
        held = contextlib.AsyncExitStack()
        try:
            hold = await held.enter_async_context(db.held("schedule", job.kind))
        except BusyError:
            continue  # another scheduler is deciding or running it
        async with held:
            if not await _due(db, job, now):
                continue
            log.info("scheduler: %s is due", job.kind)
            try:
                await job.run(db, hold)
            except BusyError:
                log.info("scheduler: %s waits, another job holds the run lock", job.kind)
                continue
            except Exception:
                # The run record keeps the error; the status line shows it.
                log.exception("scheduler: %s failed", job.kind)
            ran.append(job.kind)
    return ran


async def run_forever(
    db: DatabaseGate,
    config: Config,
    gateway: GatewayFactory = default_gateway,
    source: PublicationSource | None = None,
) -> None:
    """Tick until cancelled. A tick that fails (a lost database) is retried on the next."""
    todo = jobs(config, gateway, source)
    log.info("scheduler on: %s", ", ".join(f"{j.kind} {j.cron!r}" for j in todo))
    while True:
        try:
            await tick(db, todo, datetime.now(config.schedule.zone))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
