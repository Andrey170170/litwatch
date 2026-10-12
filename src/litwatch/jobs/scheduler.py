"""The in-process scheduler: the nightly and weekly runs at their cron times, in the
configured timezone. Off unless `[schedule] enabled = true`; nothing runs on a schedule
before cutover (spec decision 22).

It keeps no state of its own. A job is due when its cron expression fired after the job's
latest run on record, so a restart neither repeats a run nor forgets one. A fire missed by
more than `CATCH_UP` is skipped rather than run late. A job that finds the run lock taken
(another job is still going) stays due and is tried again on the next tick.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from croniter import croniter

from litwatch import store
from litwatch.config import Config
from litwatch.jobs import nightly, reviews
from litwatch.jobs.context import GatewayFactory, default_gateway, model_run
from litwatch.jobs.lock import BusyError, run_lock

log = logging.getLogger(__name__)

CATCH_UP = timedelta(hours=6)
TICK_SECONDS = 30
# Held per job kind while deciding and running, so two schedulers never run one fire twice.
SCHEDULE_LOCK = 7213913


@dataclass(frozen=True)
class Job:
    kind: str  # the run kind it records, which is also how its last run is found
    cron: str
    run: Callable[[psycopg.AsyncConnection], Awaitable[object]]


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


def jobs(config: Config, gateway: GatewayFactory = default_gateway) -> list[Job]:
    async def run_nightly(conn: psycopg.AsyncConnection) -> object:
        return await nightly.run(conn, config, gateway=gateway)

    async def run_weekly(conn: psycopg.AsyncConnection) -> object:
        async with run_lock(conn), model_run(conn, config, "weekly", gateway=gateway) as run:
            return await reviews.weekly(conn, config, run)

    return [
        Job("nightly", config.schedule.nightly, run_nightly),
        Job("weekly", config.schedule.weekly, run_weekly),
    ]


async def _last_run(conn: psycopg.AsyncConnection, kind: str) -> datetime | None:
    row = await (
        await conn.execute("SELECT max(started_at) AS at FROM runs WHERE kind = %s", (kind,))
    ).fetchone()
    return row["at"]


async def tick(conn: psycopg.AsyncConnection, todo: list[Job], now: datetime) -> list[str]:
    """Run every job that is due at `now`, in order. Returns the kinds that ran."""
    ran = []
    for job in todo:
        key = (SCHEDULE_LOCK, job.kind)
        row = await (
            await conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS locked", key)
        ).fetchone()
        if not row["locked"]:
            continue  # another scheduler is deciding or running it
        try:
            if not is_due(job.cron, now, await _last_run(conn, job.kind)):
                continue
            log.info("scheduler: %s is due", job.kind)
            try:
                await job.run(conn)
            except BusyError:
                log.info("scheduler: %s waits, another job holds the run lock", job.kind)
                continue
            except Exception:
                # The run record keeps the error; the status line shows it.
                log.exception("scheduler: %s failed", job.kind)
            ran.append(job.kind)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", key)
    return ran


async def run_forever(dsn: str, config: Config, gateway: GatewayFactory = default_gateway) -> None:
    """Tick until cancelled. Each tick opens its own connection, so a lost database is
    retried rather than fatal."""
    todo = jobs(config, gateway)
    log.info("scheduler on: nightly %r, weekly %r", config.schedule.nightly, config.schedule.weekly)
    while True:
        try:
            async with await store.connect(dsn) as conn:
                await tick(conn, todo, datetime.now(config.schedule.zone))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
