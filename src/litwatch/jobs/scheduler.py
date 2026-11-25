"""The schedule (docs/plans/a2-work-gate.md, P3): the nightly run and the publication lookup at
their cron times, in the configured timezone, each fire a work item. Off unless `[schedule]
enabled = true`; nothing runs on a schedule before cutover (spec decision 22).

The scheduler only submits. Its Worker (`ScheduleWorker`, the one item of kind `schedule`)
submits each fire of each job that is due, then waits for the next fire. A fire is due from its
time until `CATCH_UP` after it, unless a run of its kind started since (one the CLI ran, or the
scheduler before the Work gate): a missed one is submitted late, one missed by longer is
skipped. Its item's key is the fire's time, which holds for good (`store.work.submit`,
`lasting`), so a fire is submitted once, however many processes or restarts look at it.

A fire's Worker (`NightlyWorker`, `PublicationsWorker`) runs its job, unless a run served it
meanwhile (`store.runs.served`): one of its kind started after the fire that closed without an
error (a nightly run by hand while the fire waited, or the fire's own earlier attempt, only its
item's end lost). One cut off (the database lost, its process or the service stopped, its close
failed) served nothing: the fire runs its job again, whose steps take only what is still due.
A nightly takes the run lock, and one that finds it taken waits `WAIT` and tries again, so it
stays pending meanwhile; once it lets go of the lock, the chunks it held back are brought
forward (`jobs.nightly.wake`).

A fire's publication and a job chunk's admission are one decision: both take the `fire` lock,
and a chunk counts a nightly fire due but not published yet as a nightly that waits
(`unpublished`), so a due nightly goes first, or the chunk does and the nightly waits that
chunk. An expression that never fires again, or never fired, is left out; the others go on."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from croniter import CroniterBadDateError, croniter

from litwatch.config import Config
from litwatch.core.work import WorkItem
from litwatch.gates.database import BusyError, DatabaseGate, LeaseLostError, ReadUnit
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
    fires, in that zone; a fall-back's repeated hour is stepped through rather than skipped."""
    return croniter(cron, now).get_next(datetime)


def _last(cron: str, now: datetime) -> datetime | None:
    """`last_fire`, None for an expression that never fired (one bound to a later year)."""
    try:
        return last_fire(cron, now)
    except CroniterBadDateError:
        return None


def _next(cron: str, now: datetime) -> datetime | None:
    """`next_fire`, None for an expression that never fires again (one bound to a past year)."""
    try:
        return next_fire(cron, now)
    except CroniterBadDateError:
        return None


def is_due(cron: str, now: datetime, last_run: datetime | None) -> bool:
    """Whether the latest fire at `now` is due: within `CATCH_UP` of it, and no run of its kind
    started (`last_run`) since. One that never fired is not."""
    if (fired := _last(cron, now)) is None:
        return False
    if now.astimezone(UTC) - fired.astimezone(UTC) > CATCH_UP:
        return False
    return last_run is None or last_run.astimezone(UTC) < fired.astimezone(UTC)


def soonest(crons: list[str], now: datetime) -> datetime | None:
    """The soonest of the expressions' next fires after `now`, in UTC, None when none fires
    again: each converted before they are compared, as two times in the zone compare by their
    wall clocks when a fall-back's `fold` sets them apart."""
    found = [at.astimezone(UTC) for cron in crons if (at := _next(cron, now)) is not None]
    return min(found, default=None)


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
        """Submit each job's fire that is due; returns when the next one is (a day on, when
        none fires again)."""
        now = datetime.now(self.zone)
        for job in self.todo:
            if (fired := _last(job.cron, now)) is None:
                continue
            # Each fire in a unit of its own: its runs read, its item submitted, at once, under
            # the lock a chunk's admission takes (`unpublished`).
            async with self.db.write(f"schedule: {job.kind}", locks=["fire"]) as unit:
                if is_due(job.cron, now, await runs.last_started(unit, job.kind)):
                    found = await work.submit(
                        unit, job.kind, fire_key(fired), not_before=fired, lasting=True
                    )
                    log.info("scheduler: %s of %s submitted (item %s)", job.kind, fired, found.id)
        after = soonest([job.cron for job in self.todo], now)
        return Later(after or datetime.now(UTC) + timedelta(days=1))


async def unpublished(unit: ReadUnit, config: Config) -> bool:
    """Whether the schedule's nightly fire is due but its item is not submitted yet: the
    scheduler is about to publish it, or late. Read under the `fire` lock, which publishing a
    fire takes, so a chunk admitted after this read is one the nightly waits."""
    if not config.schedule.enabled:
        return False
    now = datetime.now(config.schedule.zone)
    cron = config.schedule.nightly
    if not is_due(cron, now, await runs.last_started(unit, "nightly")):
        return False
    fired = _last(cron, now)
    assert fired is not None  # it is due
    return not await work.submitted(unit, "nightly", fire_key(fired))


async def _served(db: DatabaseGate, kind: str, item: WorkItem) -> bool:
    """Whether a run served the fire `item` is (`store.runs.served`)."""
    async with db.read(f"{kind}: served already") as unit:
        return await runs.served(unit, kind, datetime.fromisoformat(item.key))


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
        try:
            async with held:
                # Decided under the run lock, which every nightly run takes.
                if not await _served(self.db, "nightly", item):
                    await nightly.locked(self.db, self.config, gateway=self.gateway)
        finally:
            await nightly.wake(self.db)  # the chunks it held back go, the run lock free
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
