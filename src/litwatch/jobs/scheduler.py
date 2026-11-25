"""The schedule (docs/plans/a2-work-gate.md, P3): the nightly run and the publication lookup at
their cron times, in the configured timezone, each fire a work item. Off unless `[schedule]
enabled = true`; nothing runs on a schedule before cutover (spec decision 22).

The scheduler only submits. Its Worker (`ScheduleWorker`, the one item of kind `schedule`)
submits each fire of each job that is due, then waits for the next fire. A fire is due from its
time until `CATCH_UP` after it, unless a run of its kind started since (one the CLI ran, or the
scheduler before the Work gate): a missed one is submitted late, one missed by longer is
skipped. Its item's key is the fire's time, which holds for good (`store.work.submit`,
`lasting`), so a fire is submitted once, however many processes or restarts look at it.

A fire's Worker (`NightlyWorker`, `PublicationsWorker`) runs its job, unless a run of its kind
started after the fire meanwhile, which served it: a nightly run by hand while the fire waited,
or the fire's own first try, cut off as its process died. A nightly takes the run lock, and one
that finds it taken waits `WAIT` and tries again, so it stays pending meanwhile, and no job's
chunk starts while it waits (`store.jobs.nightly_waits`)."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from croniter import croniter

from litwatch.config import Config
from litwatch.core.work import WorkItem
from litwatch.gates.database import BusyError, DatabaseGate, LeaseLostError
from litwatch.gates.work import Later, conflicted, database_lost
from litwatch.jobs import nightly, publications
from litwatch.jobs.context import GatewayFactory, default_gateway
from litwatch.publications import Dblp, PublicationSource
from litwatch.store import runs, work

log = logging.getLogger(__name__)

CATCH_UP = timedelta(hours=6)
# How long a nightly that finds the run lock taken waits before it tries again.
WAIT = timedelta(seconds=30)
# The scheduler's own work item: one, which waits for the next fire.
SCHEDULE = "schedule"


@dataclass(frozen=True)
class Job:
    kind: str  # its fires' work kind, and the run kind its runs record
    cron: str


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
    """Whether the latest fire at `now` is due: within `CATCH_UP` of it, and no run of its kind
    started (`last_run`) since."""
    fired = last_fire(cron, now).astimezone(UTC)
    if now.astimezone(UTC) - fired > CATCH_UP:
        return False
    return last_run is None or last_run.astimezone(UTC) < fired


def soonest(crons: list[str], now: datetime) -> datetime:
    """The soonest of the expressions' next fires after `now`, in UTC: each converted before
    they are compared, as two times in the zone compare by their wall clocks when a fall-back's
    `fold` sets them apart."""
    return min(next_fire(cron, now).astimezone(UTC) for cron in crons)


def jobs(config: Config) -> list[Job]:
    return [
        Job("nightly", config.schedule.nightly),
        Job("publications", config.schedule.publications),
    ]


def fire_key(fired: datetime) -> str:
    """A fire's work item key: its time, in UTC. One fire per catch-up, the latest: fires missed
    while nothing looked are not each made up, only the last of them within `CATCH_UP`."""
    return fired.astimezone(UTC).isoformat()


class ScheduleWorker:
    """Submits the fires of `config`'s schedule as they come."""

    def __init__(self, db: DatabaseGate, config: Config) -> None:
        self.db, self.todo = db, jobs(config)
        self.zone = config.schedule.zone

    async def sweep(self) -> None:
        """As `serve` starts: the fires due submitted before any other item can start, so no
        job's chunk goes ahead of a nightly due; then the scheduler's item, for the next fire."""
        after = await self.fires()
        async with self.db.write("schedule: the next fire") as unit:
            await work.submit(unit, SCHEDULE, SCHEDULE, not_before=after.at)

    async def __call__(self, item: WorkItem) -> Later:
        """Submit the fires due, then wait for the next. One that fails waits `WAIT` and looks
        again rather than ending the item, which would leave the schedule with none."""
        try:
            return await self.fires()
        except Exception as exc:
            if database_lost(exc) or conflicted(exc) or isinstance(exc, LeaseLostError):
                raise  # the gate tries the item again
            log.exception("scheduler: submitting the fires failed")
            return Later(datetime.now(UTC) + WAIT)

    async def fires(self) -> Later:
        """Submit each job's fire that is due; returns when the next one is."""
        now = datetime.now(self.zone)
        for job in self.todo:
            fired = last_fire(job.cron, now)
            # Each fire in a unit of its own: its runs read, its item submitted, at once.
            async with self.db.write(f"schedule: {job.kind}") as unit:
                if is_due(job.cron, now, await runs.last_started(unit, job.kind)):
                    found = await work.submit(
                        unit, job.kind, fire_key(fired), not_before=fired, lasting=True
                    )
                    log.info("scheduler: %s of %s submitted (item %s)", job.kind, fired, found.id)
        return Later(soonest([job.cron for job in self.todo], now))


async def _served(db: DatabaseGate, kind: str, item: WorkItem) -> bool:
    """Whether a run of `kind` started at or after the fire `item` is, which served it."""
    async with db.read(f"{kind}: served already") as unit:
        last = await runs.last_started(unit, kind)
    return last is not None and last >= datetime.fromisoformat(item.key)


class NightlyWorker:
    """Runs the nightly of each fire, under the run lock, through `gateway`'s models."""

    def __init__(
        self, db: DatabaseGate, config: Config, gateway: GatewayFactory = default_gateway
    ) -> None:
        self.db, self.config, self.gateway = db, config, gateway

    async def __call__(self, item: WorkItem) -> Later | None:
        held = AsyncExitStack()
        try:
            await held.enter_async_context(self.db.held("run"))
        except BusyError:
            log.info("scheduler: the nightly waits, another job holds the run lock")
            return Later(datetime.now(UTC) + WAIT)
        async with held:
            # Decided under the run lock, which every nightly run takes.
            if await _served(self.db, "nightly", item):
                return None
            await nightly.locked(self.db, self.config, gateway=self.gateway)
        return None


class PublicationsWorker:
    """Looks up the library's publications on `source` (DBLP), for each fire. It calls no model
    and takes no run lock; one item of its kind runs at a time, the CLI's too."""

    def __init__(self, db: DatabaseGate, source: PublicationSource | None = None) -> None:
        self.db = db
        self.source: PublicationSource = source or Dblp()

    async def __call__(self, item: WorkItem) -> None:
        if not await _served(self.db, "publications", item):
            await publications.run(self.db, self.source)
