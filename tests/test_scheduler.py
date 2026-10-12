"""The scheduler: when a job is due, and that a restart neither repeats nor forgets a run."""

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from litwatch import store
from litwatch.api import create_app
from litwatch.config import Config, Schedule
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


async def test_a_job_another_scheduler_is_deciding_on_is_left_to_it(conn):
    ran = []

    async def run(c):
        ran.append("nightly")

    todo = [scheduler.Job("nightly", "0 6 * * *", run)]
    async with await store.sibling(conn) as other:
        await other.execute(
            "SELECT pg_advisory_lock(%s, hashtext('nightly'))", (scheduler.SCHEDULE_LOCK,)
        )
        assert await scheduler.tick(conn, todo, at(6, 1)) == []
    assert await scheduler.tick(conn, todo, at(6, 1)) == ["nightly"]


async def test_shutdown_waits_for_the_job_in_progress_to_finish_its_record(monkeypatch):
    recorded = []

    async def forever(*args):
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0)  # a job's cleanup awaits, as model_run's does
            recorded.append("finished")

    monkeypatch.setattr(scheduler, "run_forever", forever)
    app = create_app("postgresql://unused", "t", Config(schedule=Schedule(enabled=True)))
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
    assert recorded == ["finished"]


def test_a_bad_cron_expression_is_refused_at_load():
    with pytest.raises(ValidationError, match="not a cron expression"):
        Schedule(nightly="every night")
