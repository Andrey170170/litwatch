"""The scheduler (docs/plans/a2-work-gate.md, P3): when a fire is due; that each is submitted
once as a work item, within its catch-up window, and never once a run of its kind served it;
that a nightly blocked by the run lock stays pending; and that `serve` runs the fires through its
Work gate and hands a running one back as it stops."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import psycopg
import pytest
from pydantic import ValidationError

from litwatch.config import Schedule
from litwatch.core.work import ItemState, WorkItem
from litwatch.gates.work import Later, WorkGate
from litwatch.jobs import nightly, scheduler
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import runs, work
from tests import settling
from tests.conftest import TREE, gate_on, paper, started
from tests.scripted import CONFIG, Scripted
from tests.sessions import Session, in_read, in_write
from tests.working import (
    QUIET,
    items_of,
    job_of,
    on_tonight,
    served,
    serving,
    stranded,
    until,
)

DAY = datetime(2026, 10, 12, tzinfo=UTC)  # a Monday
ZONE = CONFIG.schedule.zone


def at(hour, minute=0, days=0):
    return DAY + timedelta(days=days, hours=hour, minutes=minute)


def test_a_job_is_due_once_after_its_time_and_not_long_after():
    nightly = "0 6 * * *"
    assert scheduler.due_fire(nightly, at(6, 10), None) == at(6, 0)
    assert scheduler.due_fire(nightly, at(6, 10), last_run=at(6, 0, days=-1)) == at(6, 0)
    ran = at(6) + timedelta(seconds=5)
    assert not scheduler.due_fire(nightly, at(6, 10), last_run=ran)  # ran already
    assert not scheduler.due_fire(nightly, at(6, 10), last_run=at(6))  # ran at its time
    assert not scheduler.due_fire(nightly, at(5, 59), last_run=at(6, 0, days=-1))  # not yet
    assert not scheduler.due_fire(nightly, at(13, 0), last_run=None)  # missed by too long
    weekly = "0 7 * * 1"
    assert scheduler.due_fire(weekly, at(7, 1), last_run=at(7, 0, days=-7)) == at(7, 0)
    assert not scheduler.due_fire(weekly, at(7, 1, days=1), last_run=at(7, 0, days=-7))


def test_the_repeated_hour_of_a_fall_back_is_scheduled_like_any_other():
    denver = ZoneInfo("America/Denver")
    second = datetime(2026, 11, 1, 1, 15, 10, tzinfo=denver, fold=1)  # 01:15 MST, after 01:59 MDT
    assert scheduler.last_fire("* * * * *", second).astimezone(UTC) == datetime(
        2026, 11, 1, 8, 15, tzinfo=UTC
    )
    before_fallback = datetime(2026, 11, 1, 1, 59, tzinfo=denver)  # MDT, an hour earlier
    assert scheduler.due_fire("* * * * *", second, last_run=before_fallback)


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
    # Within a second, the next second: `now` is taken as it is, not rounded.
    every_second = at(8).replace(microsecond=999999)
    assert scheduler.next_fire("* * * * * *", every_second) == at(8) + timedelta(seconds=1)


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
    item found served when it runs (its end lost after its run closed, say) runs nothing."""
    config = schedule(daily(timedelta(minutes=2)))
    fired = scheduler.last_fire(config.schedule.nightly, datetime.now(ZONE))
    await in_write(conn, runs.finish, await started(conn, "nightly"))
    await scheduler.ScheduleWorker(db, config)(ITEM)
    assert await items_of(conn, "nightly", scheduler.fire_key(fired)) == []

    async def never(*args, **kwargs):
        raise AssertionError("a served fire ran")

    monkeypatch.setattr(nightly, "locked", never)
    async with db.write("a fire's item") as unit:
        item = await work.submit(unit, "nightly", scheduler.fire_key(fired), lasting=True)
    assert await scheduler.NightlyWorker(db, config)(item) is None


@pytest.mark.parametrize("ended", ["failed", "still open"])
async def test_a_run_of_this_image_that_has_not_closed_cleanly_leaves_the_fire_due(
    conn: Session, db, ended
):
    """A nightly by hand after the fire, in a command item of this image, failed or not closed
    yet: it serves no fire yet, so the fire is submitted (and, the lookup's too), and a chunk
    counts it as due meanwhile; the fire's item finds out under the run lock what that run
    did."""
    cron = daily(timedelta(minutes=2))  # once, so both fires are one
    config = schedule(cron, cron)
    fired = scheduler.last_fire(config.schedule.nightly, datetime.now(ZONE))
    for kind in ("nightly", "publications"):
        run = await started(conn, kind, alive=True)
        if ended == "failed":
            await in_write(conn, runs.finish, run, error="RuntimeError: it broke")
    assert await in_read(conn, scheduler.unpublished, config)
    await scheduler.ScheduleWorker(db, config)(ITEM)
    for kind in ("nightly", "publications"):
        (item,) = await items_of(conn, kind, scheduler.fire_key(fired))
        assert item["state"] == "pending"


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


@pytest.mark.parametrize("year", ["2025", "2099"])
async def test_an_expression_that_never_fires_again_or_never_fired_leaves_the_others_going(
    conn: Session, db, year
):
    """The nightly bound to a year past (it never fires again) or to come (it never fired): it
    is left out, and `serve`'s startup sweep goes on: the lookup due is submitted, and the
    scheduler's item waits for the lookup's next fire."""
    config = schedule(f"0 9 24 11 * 0 {year}", daily(timedelta(minutes=2)))
    now = datetime.now(ZONE)
    fired = scheduler.last_fire(config.schedule.publications, now)
    await scheduler.ScheduleWorker(db, config).sweep()
    (item,) = await items_of(conn, "publications", scheduler.fire_key(fired))
    assert item["state"] == "pending"
    (own,) = await items_of(conn, scheduler.SCHEDULE, scheduler.SCHEDULE)
    assert own["not_before"] == scheduler.next_fire(config.schedule.publications, now)
    assert not await in_read(conn, jobs_store.nightly_waits)
    assert not await in_read(conn, scheduler.unpublished, config)


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


@pytest.mark.parametrize("through", ["the fire", "the CLI"])
async def test_a_nightly_brings_forward_the_chunks_it_held_back_as_it_lets_go(
    conn: Session, db, monkeypatch, through
):
    """A chunk that found the nightly due waits `worker.WAIT`; the nightly, its run lock let go,
    brings it forward to when it is due, whether a fire or the CLI ran it."""
    monkeypatch.setattr(nightly, "locked", AsyncMock(return_value=1))
    job = await in_write(
        conn, jobs_store.create, "process", {"papers": ["2610.00001"]}, 1, "queued"
    )
    await conn.execute(  # as the chunk left it, waiting on the nightly
        "UPDATE work_items SET not_before = now() + interval '1 hour' WHERE kind = %s",
        (jobs_store.WORK,),
    )
    if through == "the fire":
        async with db.write("a fire's item") as unit:
            item = await work.submit(unit, "nightly", scheduler.fire_key(DAY), lasting=True)
        assert await scheduler.NightlyWorker(db, CONFIG)(item) is None
    else:
        await nightly.run(db, CONFIG)
    (chunk,) = await items_of(conn, jobs_store.WORK, str(job))
    assert chunk["not_before"] <= datetime.now(UTC)


LOST = psycopg.OperationalError("the server closed the connection unexpectedly")


@pytest.mark.parametrize(
    ("error", "then"),
    [
        (LOST, None),
        (RuntimeError("the release went wrong"), None),
        (RuntimeError("the release went wrong"), "a read lost"),
        (RuntimeError("the release went wrong"), "many attempts"),
    ],
    ids=["database lost", "another error", "then a read lost", "after many attempts"],
)
async def test_a_fire_whose_nightly_could_not_close_runs_it_again_and_releases_tonight(
    conn: Session, db, monkeypatch, error, then
):
    """The fire's nightly fails to close its run (and release tonight's jobs, in the same unit):
    the database lost, or any other error. Its own attempt, cut off, served nothing, so the
    same fire runs the nightly again, after a backoff, which closes, and the job on tonight's
    run goes in the queue. So too when the database is lost for the next read after it, and
    when the fire has waited many attempts before (the run lock's waits count): its backoff is
    capped before it doubles."""
    monkeypatch.setattr(scheduler, "WAIT", timedelta(milliseconds=200))
    monkeypatch.setattr(scheduler, "CLOSE_WAIT_MOST", timedelta(milliseconds=400))
    await settling.save_tree(conn, TREE)
    models = Scripted()
    monkeypatch.setattr(nightly, "locked", partial(nightly.locked, fetch=False, with_digests=False))
    job = await in_write(
        conn, jobs_store.create, "process", {"papers": ["2610.00001"]}, 1, "offered"
    )
    await on_tonight(conn, job)
    real, lost = jobs_store.release, []

    async def drops(unit):
        if not lost:
            lost.append(True)
            raise error
        await real(unit)

    monkeypatch.setattr(jobs_store, "release", drops)
    if then == "a read lost":  # the first read after it, but the gate's own
        real_read, read_lost = db.read, []

        def read(name, *args, **kwargs):
            if lost and not read_lost and not name.startswith("work:"):
                read_lost.append(name)
                raise LOST
            return real_read(name, *args, **kwargs)

        monkeypatch.setattr(db, "read", read)
    async with db.write("a fire's item") as unit:
        item = await work.submit(unit, "nightly", scheduler.fire_key(DAY), lasting=True)
    if then == "many attempts":
        await conn.execute("UPDATE work_items SET attempts = 60 WHERE id = %s", (item.id,))

    async def ended() -> bool:
        (row,) = await items_of(conn, "nightly", item.key)
        return row["state"] == "ended"

    async with serving(
        WorkGate(db, poll=0.1), {"nightly": scheduler.NightlyWorker(db, CONFIG, models.gateway)}
    ):
        await until(ended, "the fire's item to end")
    (row,) = await items_of(conn, "nightly", item.key)
    assert lost and row["outcome"] == "done"
    if then is None:
        assert row["attempts"] == 2
    first, again = await (
        await conn.execute("SELECT * FROM runs WHERE kind = 'nightly' ORDER BY id")
    ).fetchall()
    assert first["error"] is not None or first["finished_at"] is None  # cut off
    assert again["finished_at"] is not None and again["error"] is None
    assert job in await in_read(conn, jobs_store.open_ids) and await stranded(conn) == []


def test_a_failed_close_waits_longer_each_attempt_up_to_its_cap():
    assert scheduler.close_backoff(1) == scheduler.WAIT
    assert scheduler.close_backoff(3) == scheduler.WAIT * 4
    assert scheduler.close_backoff(43) == scheduler.CLOSE_WAIT_MOST


async def test_a_fire_whose_digests_lose_a_conflict_is_tried_again(conn: Session, db, monkeypatch):
    """A digest's write loses a serialization conflict inside the topics' task group: the group
    is made of retryable errors alone, so the gate tries the same fire again, which runs. Its
    first attempt's run closed failed, and released the job it held back; but the fire still
    owes its retry (the gate is slow to record it, here), so the job's chunk starts only once
    the second attempt has closed."""
    from litwatch import digests  # noqa: PLC0415
    from litwatch.jobs import worker  # noqa: PLC0415

    await settling.save_tree(conn, TREE)
    a = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes.")
    await in_write(conn, papers_store.upsert, [a])
    config = CONFIG.model_copy(update={"schedule": QUIET})  # fires on: no fire due but ours
    models = Scripted()
    monkeypatch.setattr(nightly, "locked", partial(nightly.locked, fetch=False))
    job = await in_write(conn, jobs_store.create, "process", {"papers": [a.id]}, 1, "queued")
    real_again, slowed = work.again, []

    async def again(unit, item, *args, **kwargs):
        if item.kind == "nightly" and not slowed:
            slowed.append(True)
            await asyncio.sleep(1.5)  # the chunk it released has its chance meanwhile
        return await real_again(unit, item, *args, **kwargs)

    monkeypatch.setattr(work, "again", again)
    tries = []

    async def write(*_, **__) -> None:
        tries.append(1)
        if len(tries) == 1:
            raise psycopg.errors.SerializationFailure("could not serialize access")

    async def released(unit, today) -> bool:
        return True  # the day's listing is out: each daily digest is written

    monkeypatch.setattr(digests, "write", write)
    monkeypatch.setattr(digests.store_, "released", released)
    async with db.write("a fire's item") as unit:
        item = await work.submit(unit, "nightly", scheduler.fire_key(DAY), lasting=True)

    async def ended() -> bool:
        (row,) = await items_of(conn, "nightly", item.key)
        return row["state"] == "ended"

    async def job_done() -> bool:
        return await ended() and (await job_of(conn, job))["state"] == "done"

    workers = {
        "nightly": scheduler.NightlyWorker(db, config, models.gateway),
        jobs_store.WORK: worker.JobWorker(db, config, models.gateway),
    }
    async with serving(WorkGate(db, poll=0.1), workers):
        await until(job_done, "the fire's item to end, and the job to be done")
    (row,) = await items_of(conn, "nightly", item.key)
    assert tries and slowed and (row["outcome"], row["attempts"]) == ("done", 2)
    first, second, chunk = await (
        await conn.execute("SELECT * FROM runs WHERE kind IN ('nightly', 'job') ORDER BY id")
    ).fetchall()
    assert [r["kind"] for r in (first, second, chunk)] == ["nightly", "nightly", "job"]
    assert first["error"] is not None and second["error"] is None
    assert chunk["started_at"] >= second["finished_at"]


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
        ended = await (
            await conn.execute(
                "SELECT 1 FROM work_items WHERE kind IN ('nightly', 'job') AND state <> 'ended'"
            )
        ).fetchall()
        return (await job_of(conn, job))["state"] == "done" and not ended

    async with served(db, config, gateway=models.gateway):
        await until(done, "the job to be done, its items ended")
    night, chunk = await (
        await conn.execute("SELECT * FROM runs WHERE kind IN ('nightly', 'job') ORDER BY id")
    ).fetchall()
    assert (night["kind"], chunk["kind"]) == ("nightly", "job")
    assert chunk["started_at"] >= night["finished_at"]  # the job waited for the nightly
    # Each ended done on record, with the run it worked.
    fired = scheduler.last_fire(config.schedule.nightly, datetime.now(ZONE))
    (fire,) = await items_of(conn, "nightly", scheduler.fire_key(fired))
    assert (fire["outcome"], night["work_id"]) == ("done", fire["id"])
    chunks = await items_of(conn, jobs_store.WORK, str(job))
    assert chunks and all(c["outcome"] == "done" for c in chunks)
    assert {c["run_id"] for c in chunks} == {chunk["id"]}
    (schedule_item,) = await items_of(conn, scheduler.SCHEDULE, scheduler.SCHEDULE)
    assert schedule_item["state"] == "pending" and schedule_item["not_before"] > datetime.now(UTC)


async def test_serve_runs_a_fire_that_comes_while_it_runs(conn: Session, db, monkeypatch):
    """No fire is due as `serve` starts; the nightly's comes three seconds later, while the app
    runs: the scheduler's item, waiting for it, submits it then, and the gate runs the nightly.
    The scheduler's clock is held an hour back until `serve` has started, so however slow its
    start, its sweep finds the fire still to come."""
    await settling.save_tree(conn, TREE)
    monkeypatch.setattr(nightly, "locked", partial(nightly.locked, fetch=False, with_digests=False))

    class Clock(datetime):
        behind = timedelta(hours=1)

        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - cls.behind

    monkeypatch.setattr(scheduler, "datetime", Clock)
    soon = datetime.now(ZONE) + timedelta(seconds=3)
    config = schedule(f"{soon.minute} {soon.hour} * * * {soon.second}")  # seconds last
    fired = scheduler.fire_key(soon.replace(microsecond=0))

    async def ended() -> bool:
        found = await items_of(conn, "nightly", fired)
        return bool(found) and found[0]["state"] == "ended"

    async with served(db, config, gateway=Scripted().gateway):
        assert not await (
            await conn.execute("SELECT 1 FROM work_items WHERE kind = 'nightly'")
        ).fetchall()
        Clock.behind = timedelta(0)
        await until(ended, "the fire's item to end")
    (item,) = await items_of(conn, "nightly", fired)
    (night,) = await (await conn.execute("SELECT * FROM runs WHERE kind = 'nightly'")).fetchall()
    assert item["outcome"] == "done" and night["work_id"] == item["id"]
    assert night["started_at"] >= soon.replace(microsecond=0) and night["error"] is None
    assert night["finished_at"] is not None


async def test_serve_stopping_mid_nightly_closes_its_run_and_the_next_serve_runs_its_job(
    conn: Session, db, monkeypatch
):
    """The service stops in the middle of a real (scripted) nightly: its run closes, cut off,
    before the lifespan ends, and the Process job it held back stays queued. The next `serve`
    finds the fire not served (its own attempt was cut off), runs the nightly again, then the
    job, which the nightly brings forward as it lets go of the run lock."""
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

    async with served(db, config, gateway=models.gateway):
        await until(done, "the job to be done")  # well before the chunk's `worker.WAIT`
    first, again = await (
        await conn.execute("SELECT * FROM runs WHERE kind = 'nightly' ORDER BY id")
    ).fetchall()
    assert first["id"] == night["id"] and again["error"] is None
    (chunk,) = await (await conn.execute("SELECT * FROM runs WHERE kind = 'job'")).fetchall()
    assert chunk["started_at"] >= again["finished_at"]
