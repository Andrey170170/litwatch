"""The dispatcher: order, cursors, backoff, the owner's retry and skip, and independence."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from litwatch import events, store
from litwatch.events import dispatch
from litwatch.store import outbox
from tests.conftest import deliver, start

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


class Recorder:
    """An integration that remembers what it handled and fails on the events it is told to."""

    description = "records events"

    def __init__(self, name="recorder", types=None, fail=()):
        self.name, self.types, self.fail = name, types, set(fail)
        self.seen: list[int] = []
        self.before_failing = None  # awaited just before a failure, to stage a race

    def wants(self, event):
        return self.types is None or event.type in self.types

    async def handle(self, conn, event):
        if event.id in self.fail:
            if self.before_failing:
                await self.before_failing()
            raise RuntimeError(f"cannot handle {event.id}")
        self.seen.append(event.id)


async def emit(conn, *types):
    return [await events.emit(conn, t, "2610.00001") for t in types]


async def test_an_integration_enabled_for_the_first_time_starts_at_the_end(conn):
    await emit(conn, events.PAPER_SAVED, events.GRADE_CHANGED)  # a library sync, say
    rec = Recorder()
    await start(conn, rec)
    assert await deliver(conn, rec, NOW) == 0
    later = await emit(conn, events.PAPER_SAVED)
    await deliver(conn, rec, NOW)
    assert rec.seen == later


async def test_events_arrive_in_order_and_the_cursor_moves_past_unwanted_ones(conn):
    rec = Recorder(types={events.PAPER_SAVED, events.TAKE_WRITTEN})
    await start(conn, rec)
    ids = await emit(
        conn, events.PAPER_SAVED, events.PAPER_ADDED, events.TAKE_WRITTEN, events.PAPER_ADDED
    )
    assert await deliver(conn, rec, NOW) == 4
    assert rec.seen == [ids[0], ids[2]]
    assert (await outbox.cursor(conn, rec.name)).position.id == ids[3]
    # Nothing new, nothing handed over twice.
    assert await deliver(conn, rec, NOW) == 0
    assert rec.seen == [ids[0], ids[2]]


async def test_an_event_that_commits_after_a_later_one_is_not_passed_over(conn):
    rec = Recorder()
    await start(conn, rec)
    other = await store.sibling(conn)
    try:
        async with other.transaction():
            (early,) = await emit(other, events.PAPER_SAVED)  # its id is taken, not committed
            (late,) = await emit(conn, events.PAPER_UNSAVED)  # a later id, committed first
            assert late > early
            # By id alone the dispatcher would hand over `late` and never look back.
            await dispatch.deliver(conn, rec, NOW)
            assert rec.seen == []
    finally:
        await other.close()
    await deliver(conn, rec, NOW)
    assert rec.seen == [early, late]


async def test_a_failing_event_waits_out_a_doubling_backoff_and_holds_back_later_ones(conn):
    rec = Recorder()
    await start(conn, rec)
    a, b, c = await emit(conn, *[events.PAPER_SAVED] * 3)
    rec.fail = {b}
    await deliver(conn, rec, NOW)
    cursor = await outbox.cursor(conn, rec.name)
    assert rec.seen == [a]
    assert (cursor.position.id, cursor.failing, cursor.failures) == (a, b, 1)
    assert cursor.retry_at == NOW + timedelta(seconds=30)
    assert "cannot handle" in cursor.last_error

    # Not before the backoff is over; then it fails again and waits twice as long.
    assert await deliver(conn, rec, NOW + timedelta(seconds=10)) == 0
    await deliver(conn, rec, NOW + timedelta(seconds=31))
    cursor = await outbox.cursor(conn, rec.name)
    assert cursor.failures == 2
    assert cursor.retry_at == NOW + timedelta(seconds=31 + 60)

    # Once it works, the rest follow; the error is kept for the record.
    rec.fail = set()
    await deliver(conn, rec, NOW + timedelta(hours=1))
    cursor = await outbox.cursor(conn, rec.name)
    assert rec.seen == [a, b, c]
    assert (cursor.position.id, cursor.failing, cursor.failures) == (c, None, 0)
    assert cursor.retry_at is None and cursor.last_error


async def test_the_backoff_runs_from_when_the_event_failed(conn):
    rec = Recorder()
    await start(conn, rec)
    (a,) = await emit(conn, events.PAPER_SAVED)
    rec.fail = {a}
    rec.before_failing = lambda: asyncio.sleep(0.3)  # a slow handle
    began = datetime.now(UTC)
    await deliver(conn, rec)
    cursor = await outbox.cursor(conn, rec.name)
    assert cursor.failed_at >= began + timedelta(seconds=0.3)
    assert cursor.retry_at == cursor.failed_at + timedelta(seconds=30)


async def test_backoff_is_capped_at_an_hour():
    assert dispatch.backoff(1) == timedelta(seconds=30)
    assert dispatch.backoff(3) == timedelta(seconds=120)
    assert dispatch.backoff(30) == timedelta(hours=1)


async def test_the_owner_can_retry_at_once_or_skip_the_event(conn):
    rec = Recorder()
    await start(conn, rec)
    a, b, c = await emit(conn, *[events.PAPER_SAVED] * 3)
    rec.fail = {a}
    await deliver(conn, rec, NOW)

    # Retry: tried on the next pass, backoff or not.
    assert await outbox.retry(conn, rec.name)
    rec.fail = {a, b}
    await deliver(conn, rec, NOW)
    assert rec.seen == []
    assert (await outbox.cursor(conn, rec.name)).failures == 2

    # Skip: the cursor goes past it and the rest follow.
    assert await outbox.skip(conn, rec.name) == a
    rec.fail = set()
    await deliver(conn, rec, NOW)
    cursor = await outbox.cursor(conn, rec.name)
    assert rec.seen == [b, c]
    assert (cursor.skipped, cursor.failing) == (1, None)
    # Nothing failing: nothing to retry or skip.
    assert not await outbox.retry(conn, rec.name)
    assert await outbox.skip(conn, rec.name) is None


async def test_a_skip_during_a_retry_that_then_fails_stays_skipped(conn):
    rec = Recorder()
    await start(conn, rec)
    a, b = await emit(conn, events.PAPER_SAVED, events.PAPER_SAVED)
    rec.fail = {a}
    await deliver(conn, rec, NOW)
    owner = await store.sibling(conn)
    try:
        # The owner skips `a` while the dispatcher is retrying it; the retry then fails.
        rec.before_failing = lambda: outbox.skip(owner, rec.name)
        await outbox.retry(conn, rec.name)
        await deliver(conn, rec, NOW)
    finally:
        await owner.close()
    cursor = await outbox.cursor(conn, rec.name)
    assert (cursor.position.id, cursor.failing, cursor.skipped) == (a, None, 1)
    rec.before_failing = None
    await deliver(conn, rec, NOW)
    assert rec.seen == [b]


async def test_one_failing_integration_delays_no_other(conn):
    broken, fine = Recorder("broken"), Recorder("fine")
    for rec in (broken, fine):
        await start(conn, rec)
    ids = await emit(conn, *[events.PAPER_SAVED] * 3)
    broken.fail = set(ids)
    for rec in (broken, fine):
        await deliver(conn, rec, NOW)
    assert (broken.seen, fine.seen) == ([], ids)
    lags = await outbox.cursors(conn, ["broken", "fine"])
    assert (lags["broken"].lag, lags["fine"].lag) == (3, 0)


async def test_run_forever_delivers_in_the_background(conn, monkeypatch):
    monkeypatch.setattr(dispatch, "IDLE_SECONDS", 0.02)
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    rec = Recorder()
    await start(conn, rec)
    task = asyncio.create_task(dispatch.run_forever(dsn, [rec]))
    try:
        ids = await emit(conn, events.PAPER_SAVED, events.PAPER_UNSAVED)
        for _ in range(1000):
            if rec.seen == ids:
                break
            await asyncio.sleep(0.02)
        assert rec.seen == ids
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class Idler(Recorder):
    """Has idle work of its own; the first try of it fails."""

    def __init__(self):
        super().__init__("idler")
        self.idled = 0

    async def idle(self, conn):
        self.idled += 1
        if self.idled == 1:
            raise RuntimeError("idle work failed once")


async def test_an_integration_with_nothing_to_handle_gets_its_idle_call(conn, monkeypatch):
    monkeypatch.setattr(dispatch, "IDLE_SECONDS", 0.01)
    monkeypatch.setattr(dispatch, "IDLE_HOOK_SECONDS", 0.05)
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    idler = Idler()
    task = asyncio.create_task(dispatch.run_forever(dsn, [idler]))
    try:
        for _ in range(500):
            if idler.idled >= 3:
                break
            await asyncio.sleep(0.01)
        # Called again after its first call failed: a failure there does not stick.
        assert idler.idled >= 3
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert not await dispatch.idle(conn, Recorder())  # no hook, nothing to call
