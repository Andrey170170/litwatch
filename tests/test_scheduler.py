"""The scheduler (docs/plans/a2-work-gate.md, P3): when a fire is due; that each is submitted
once as a work item, within its catch-up window, and never once a run of its kind served it;
that a nightly blocked by the run lock stays pending; and that `serve` runs the fires through its
Work gate and hands a running one back as it stops."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from litwatch.config import Schedule
from litwatch.core.work import ItemState, WorkItem
from litwatch.gates.work import Later, WorkGate
from litwatch.jobs import nightly, scheduler, worker
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import work
from tests import settling
from tests.conftest import TREE, gate_on, paper, started
from tests.scripted import CONFIG, Scripted
from tests.sessions import Session, in_read, in_write
from tests.working import QUIET, items_of, job_of, served, serving, until

DAY = datetime(2026, 10, 12, tzinfo=UTC)  # a Monday
ZONE = CONFIG.schedule.zone


def at(hour, minute=0, days=0):
    return DAY + timedelta(days=days, hours=hour, minutes=minute)


def test_a_job_is_due_once_after_its_time_and_not_long_after():
    nightly = "0 6 * * *"
    assert scheduler.is_due(nightly, at(6, 10), None)
    assert scheduler.is_due(nightly, at(6, 10), last_run=at(6, 0, days=-1))
    assert not scheduler.is_due(nightly, at(6, 10), last_run=at(6, 0, 5))  # ran already
    assert not scheduler.is_due(nightly, at(5, 59), last_run=at(6, 0, days=-1))  # not yet
    assert not scheduler.is_due(nightly, at(13, 0), last_run=None)  # missed by too long
    weekly = "0 7 * * 1"
    assert scheduler.is_due(weekly, at(7, 1), last_run=at(7, 0, days=-7))
    assert not scheduler.is_due(weekly, at(7, 1, days=1), last_run=at(7, 0, days=-7))


def test_the_repeated_hour_of_a_fall_back_is_scheduled_like_any_other():
    denver = ZoneInfo("America/Denver")
    second = datetime(2026, 11, 1, 1, 15, 10, tzinfo=denver, fold=1)  # 01:15 MST, after 01:59 MDT
    assert scheduler.last_fire("* * * * *", second).astimezone(UTC) == datetime(
        2026, 11, 1, 8, 15, tzinfo=UTC
    )
    before_fallback = datetime(2026, 11, 1, 1, 59, tzinfo=denver)  # MDT, an hour earlier
    assert scheduler.is_due("* * * * *", second, last_run=before_fallback)


def test_the_next_fire_steps_through_a_fall_back_in_the_zone():
    denver = ZoneInfo("America/Denver")
    # 01:45 MDT on the morning the clocks go back: 01:30 comes round again an hour later.
    first = datetime(2026, 11, 1, 1, 45, tzinfo=denver)
    again = scheduler.next_fire("30 1 * * *", first)
    assert again.astimezone(UTC) == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)  # 01:30 MST
    assert again.fold == 1 and again.tzinfo is denver
    # After the repeated half hour, the next one is the day after.
    later = scheduler.next_fire("30 1 * * *", again)
    assert later.astimezone(UTC) == datetime(2026, 11, 2, 8, 30, tzinfo=UTC)
    # Exactly on a fire the next one is the one after; a weekly fires on its day.
    assert scheduler.next_fire("0 6 * * *", at(6, 0)) == at(6, 0, days=1)
    assert scheduler.next_fire("0 7 * * 1", at(7, 1)) == at(7, 0, days=7)


def test_the_soonest_fire_of_two_is_compared_in_utc_through_a_fall_back():
    """01:30 MDT on the morning the clocks go back: 01:45 MDT comes before 01:15 MST, which
    reads earlier on the wall clock."""
    now = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/Denver"))
    soonest = scheduler.soonest(["15 1 * * *", "45 1 * * *"], now)
    assert soonest == datetime(2026, 11, 1, 7, 45, tzinfo=UTC)


def test_a_bad_cron_expression_is_refused_at_load():
    for job in ("nightly", "publications"):
        with pytest.raises(ValidationError, match="not a cron expression"):
            Schedule(**{job: "every night"})
    with pytest.raises(ValidationError, match="not a time zone"):
        Schedule(timezone="Mars/Olympus_Mons")


# -- fires as work items ---------------------------------------------------------------------


def daily(ago: timedelta) -> str:
    """A daily cron expression that last fired `ago` before now, in the configured zone."""
    fired = datetime.now(ZONE) - ago
    return f"{fired.minute} {fired.hour} * * *"


def schedule(nightly: str, publications: str = QUIET.publications):
    return CONFIG.model_copy(
        update={"schedule": Schedule(enabled=True, nightly=nightly, publications=publications)}
    )


ITEM = WorkItem(  # the scheduler's own item, which it does not read
    id=0, kind=scheduler.SCHEDULE, key=scheduler.SCHEDULE, admission=scheduler.SCHEDULE,
    state=ItemState.RUNNING, not_before=DAY, attempts=1,
)  # fmt: skip


async def test_a_fire_is_submitted_once_within_its_catch_up_window_and_never_after(
    conn: Session, db
):
    """The nightly fired two minutes ago: its item is submitted, keyed by the fire, and looking
    again (another process, a restart) finds it, even once it has ended. The lookup fired seven
    hours ago, past `CATCH_UP`: skipped. The scheduler then waits for the next fire."""
    config = schedule(daily(timedelta(minutes=2)), daily(timedelta(hours=7)))
    fired = scheduler.last_fire(config.schedule.nightly, datetime.now(ZONE))
    waits = await scheduler.ScheduleWorker(db, config)(ITEM)
    assert isinstance(waits, Later)
    tomorrow = [scheduler.next_fire(j.cron, datetime.now(ZONE)) for j in scheduler.jobs(config)]
    assert waits.at == min(tomorrow).astimezone(UTC)
    (item,) = await items_of(conn, "nightly", scheduler.fire_key(fired))
    assert (item["state"], item["lasting"], item["not_before"]) == ("pending", True, fired)
    assert await in_read(conn, jobs_store.nightly_waits)  # no chunk starts meanwhile

    await conn.execute("UPDATE work_items SET state = 'ended', outcome = 'done'")
    async with gate_on(conn) as other:  # another process, or this one restarted
        await scheduler.ScheduleWorker(other, config)(ITEM)
    assert len(await items_of(conn, "nightly", scheduler.fire_key(fired))) == 1
    assert await items_of(conn, "publications", scheduler.fire_key(fired)) == []
    assert not await (
        await conn.execute("SELECT 1 FROM work_items WHERE kind = 'publications'")
    ).fetchall()


async def test_a_fire_a_run_served_already_is_neither_submitted_nor_run(
    conn: Session, db, monkeypatch
):
    """A nightly started after the fire (by hand, or by the scheduler before the Work gate)
    served it: the scheduler, which reads the runs before the key, submits nothing; and a fire's
    item found served when it runs (its first try cut off, say) runs nothing."""
    config = schedule(daily(timedelta(minutes=2)))
    fired = scheduler.last_fire(config.schedule.nightly, datetime.now(ZONE))
    await started(conn, "nightly")
    await scheduler.ScheduleWorker(db, config)(ITEM)
    assert await items_of(conn, "nightly", scheduler.fire_key(fired)) == []

    async def never(*args, **kwargs):
        raise AssertionError("a served fire ran")

    monkeypatch.setattr(nightly, "locked", never)
    async with db.write("a fire's item") as unit:
        item = await work.submit(unit, "nightly", scheduler.fire_key(fired), lasting=True)
    assert await scheduler.NightlyWorker(db, config)(item) is None


async def test_a_run_started_before_a_fire_does_not_serve_it(conn: Session, db, monkeypatch):
    """A nightly run by hand before the fire's time: the fire is submitted, and its item runs
    the nightly. Only a run started at or after a fire serves it."""
    config = schedule(daily(timedelta(minutes=2)))
    fired = scheduler.last_fire(config.schedule.nightly, datetime.now(ZONE))
    before = await started(conn, "nightly")
    await conn.execute(
        "UPDATE runs SET started_at = %s, finished_at = %s WHERE id = %s",
        (fired - timedelta(minutes=1), fired - timedelta(seconds=30), before),
    )
    await scheduler.ScheduleWorker(db, config)(ITEM)
    (row,) = await items_of(conn, "nightly", scheduler.fire_key(fired))
    ran = []

    async def runs_it(*args, **kwargs):
        ran.append("the fire's nightly")

    monkeypatch.setattr(nightly, "locked", runs_it)
    item = WorkItem(**{k: row[k] for k in ("id", "kind", "key", "admission", "not_before")},
                    state=ItemState.RUNNING, attempts=1)  # fmt: skip
    assert await scheduler.NightlyWorker(db, config)(item) is None
    assert ran == ["the fire's nightly"]


async def test_the_scheduler_that_fails_looks_again_rather_than_ends_its_item(db, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no such zone today")

    monkeypatch.setattr(scheduler, "last_fire", broken)
    worker = scheduler.ScheduleWorker(db, schedule(daily(timedelta(minutes=2))))
    waits = await worker(ITEM)
    assert isinstance(waits, Later) and waits.at > datetime.now(UTC)


async def test_a_nightly_blocked_by_the_run_lock_stays_pending_then_runs(
    conn: Session, db, monkeypatch
):
    """Another process holds the run lock (a job's chunk, the CLI's nightly): the fire's item
    waits, pending, and holds back every chunk meanwhile; the lock let go, it runs."""
    monkeypatch.setattr(scheduler, "WAIT", timedelta(milliseconds=200))
    await settling.save_tree(conn, TREE)
    models = Scripted()
    monkeypatch.setattr(nightly, "locked", partial(nightly.locked, fetch=False, with_digests=False))
    async with db.write("a fire's item") as unit:
        item = await work.submit(unit, "nightly", scheduler.fire_key(DAY), lasting=True)
    workers = {"nightly": scheduler.NightlyWorker(db, CONFIG, models.gateway)}
    gate = WorkGate(db, poll=0.1)

    async def tried() -> bool:
        (row,) = await items_of(conn, "nightly", item.key)
        return row["attempts"] >= 2 and row["state"] == "pending"

    async def ran() -> bool:
        (row,) = await items_of(conn, "nightly", item.key)
        return row["state"] == "ended"

    async with gate_on(conn) as other, contextlib.AsyncExitStack() as lock:
        await lock.enter_async_context(other.held("run"))
        async with serving(gate, workers):
            await until(tried, "the nightly to find the run lock taken, twice")
            assert await in_read(conn, jobs_store.nightly_waits)
            assert not await (await conn.execute("SELECT 1 FROM runs")).fetchall()
            await lock.aclose()  # the other process lets the lock go
            await until(ran, "the nightly to run")
    (row,) = await items_of(conn, "nightly", item.key)
    assert row["outcome"] == "done"
    (run,) = await (await conn.execute("SELECT * FROM runs WHERE kind = 'nightly'")).fetchall()
    assert run["finished_at"] is not None and run["error"] is None
    assert not await in_read(conn, jobs_store.nightly_waits)


# -- through `serve` -------------------------------------------------------------------------


async def test_serve_runs_the_due_nightly_then_the_job_it_held_back(conn: Session, db, monkeypatch):
    """`serve`'s lifespan sweeps the schedule, which submits the fire due before the gate
    starts; the fire runs the nightly as a Worker, and the Process job queued before goes once
    it has ended, not before."""
    await settling.save_tree(conn, TREE)
    a = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes.")
    await in_write(conn, papers_store.upsert, [a])
    models = Scripted()
    monkeypatch.setattr(nightly, "locked", partial(nightly.locked, fetch=False, with_digests=False))
    config = schedule(daily(timedelta(minutes=2)))
    job = await in_write(conn, jobs_store.create, "process", {"papers": [a.id]}, 1, "queued")

    async def done() -> bool:
        return (await job_of(conn, job))["state"] == "done"

    async with served(db, config, gateway=models.gateway):
        await until(done, "the job to be done")
    night, chunk = await (
        await conn.execute("SELECT * FROM runs WHERE kind IN ('nightly', 'job') ORDER BY id")
    ).fetchall()
    assert (night["kind"], chunk["kind"]) == ("nightly", "job")
    assert chunk["started_at"] >= night["finished_at"]  # the job waited for the nightly
    (schedule_item,) = await items_of(conn, scheduler.SCHEDULE, scheduler.SCHEDULE)
    assert schedule_item["state"] == "pending" and schedule_item["not_before"] > datetime.now(UTC)


async def test_serve_stopping_mid_nightly_closes_its_run_and_the_next_serve_runs_its_job(
    conn: Session, db, monkeypatch
):
    """The service stops in the middle of a real (scripted) nightly: its run closes, cut off,
    before the lifespan ends, and the Process job it held back stays queued. The next `serve`
    finds the fire served by that run, runs no nightly again, and runs the job."""
    await settling.save_tree(conn, TREE)
    a = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes.")
    await in_write(conn, papers_store.upsert, [a])
    models = Scripted()
    monkeypatch.setattr(nightly, "locked", partial(nightly.locked, fetch=False, with_digests=False))
    classify, in_steps = nightly.classify, asyncio.Event()

    async def held(*args, **kwargs):
        in_steps.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(nightly, "classify", held)
    config = schedule(daily(timedelta(minutes=2)))
    job = await in_write(conn, jobs_store.create, "process", {"papers": [a.id]}, 1, "queued")
    async with served(db, config, gateway=models.gateway):
        await asyncio.wait_for(in_steps.wait(), 10)
    (night,) = await (await conn.execute("SELECT * FROM runs WHERE kind = 'nightly'")).fetchall()
    assert night["finished_at"] is not None and "CancelledError" in night["error"]
    (item,) = await (
        await conn.execute("SELECT * FROM work_items WHERE kind = 'nightly'")
    ).fetchall()
    assert item["state"] == "pending" and (await job_of(conn, job))["state"] == "queued"

    monkeypatch.setattr(nightly, "classify", classify)

    async def done() -> bool:
        return (await job_of(conn, job))["state"] == "done"

    # The chunk may look while the served fire's item is still open: it looks again soon.
    monkeypatch.setattr(worker, "WAIT", timedelta(milliseconds=200))
    async with served(db, config, gateway=models.gateway):
        await until(done, "the job to be done")
    nights = await (await conn.execute("SELECT * FROM runs WHERE kind = 'nightly'")).fetchall()
    assert [n["id"] for n in nights] == [night["id"]]  # the fire was served: no second nightly
    (chunk,) = await (await conn.execute("SELECT * FROM runs WHERE kind = 'job'")).fetchall()
    assert chunk["started_at"] >= night["finished_at"]
