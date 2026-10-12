"""The scheduler: when a job is due, and that a restart neither repeats nor forgets a run."""

from datetime import UTC, datetime, timedelta

from litwatch.jobs import scheduler
from litwatch.jobs.lock import BusyError

DAY = datetime(2026, 10, 12, tzinfo=UTC)  # a Monday


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


async def test_a_tick_runs_what_is_due_and_retries_a_job_that_finds_the_lock_taken(conn):
    ran = []

    async def runs(kind):
        async def run(c):
            ran.append(kind)
            await c.execute("INSERT INTO runs (kind, started_at) VALUES (%s, %s)", (kind, at(6, 1)))

        return run

    async def busy(c):
        raise BusyError("another job")

    todo = [
        scheduler.Job("nightly", "0 6 * * *", await runs("nightly")),
        scheduler.Job("weekly", "0 6 * * 1", busy),
    ]
    assert await scheduler.tick(conn, todo, at(6, 1)) == ["nightly"]
    # The nightly run is on record now; the weekly one waits for the lock.
    assert await scheduler.tick(conn, todo, at(6, 2)) == []
    assert ran == ["nightly"]
