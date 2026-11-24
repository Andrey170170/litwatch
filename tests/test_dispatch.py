"""Delivery: order, cursors, backoff, the owner's retry and skip, independence, and each turn
as a work item of the Work gate."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import events
from litwatch.api import create_app
from litwatch.events import dispatch
from litwatch.events.worker import DeliveryWorker
from litwatch.gates.database import outside_unit
from litwatch.gates.work import WorkGate
from litwatch.integrations.deep import Deep, DeepClient
from litwatch.store import outbox
from tests import sessions
from tests.conftest import deliver, settled, start
from tests.sessions import Session, in_read, in_write, staged
from tests.working import items_of, serving, until

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


class Recorder:
    """An integration that remembers what it handled and fails on the events it is told to."""

    description = "records events"

    def __init__(self, name="recorder", types=None, fail=()):
        self.name, self.types, self.fail = name, types, set(fail)
        self.fail_all = False  # fail on every event, armed before one commits
        self.seen: list[int] = []
        self.before_failing = None  # awaited just before a failure, to stage a race

    def wants(self, event):
        return self.types is None or event.type in self.types

    async def handle(self, db, event):
        if self.fail_all or event.id in self.fail:
            if self.before_failing:
                await self.before_failing()
            raise RuntimeError(f"cannot handle {event.id}")
        self.seen.append(event.id)


async def emit(conn: Session, *types):
    return [await in_write(conn, outbox.emit, t, "2610.00001") for t in types]


async def test_an_integration_enabled_for_the_first_time_starts_at_the_end(conn: Session, db):
    await emit(conn, events.PAPER_SAVED, events.GRADE_CHANGED)  # a library sync, say
    rec = Recorder()
    await start(db, rec)
    assert await deliver(db, rec, NOW) == 0
    later = await emit(conn, events.PAPER_SAVED)
    await deliver(db, rec, NOW)
    assert rec.seen == later


async def test_events_arrive_in_order_and_the_cursor_moves_past_unwanted_ones(conn: Session, db):
    rec = Recorder(types={events.PAPER_SAVED, events.GRADE_CHANGED})
    await start(db, rec)
    ids = await emit(
        conn, events.PAPER_SAVED, events.PAPER_ADDED, events.GRADE_CHANGED, events.PAPER_ADDED
    )
    assert await deliver(db, rec, NOW) == 4
    assert rec.seen == [ids[0], ids[2]]
    assert (await in_write(conn, outbox.cursor, rec.name)).position.id == ids[3]
    # Nothing new, nothing handed over twice.
    assert await deliver(db, rec, NOW) == 0
    assert rec.seen == [ids[0], ids[2]]


async def test_an_event_that_commits_after_a_later_one_is_not_passed_over(conn: Session, db):
    rec = Recorder()
    await start(db, rec)
    other = await sessions.sibling(conn)
    try:
        async with other.transaction():
            # Its id is taken, not committed.
            early = await outbox.emit(staged(other), events.PAPER_SAVED, "2610.00001")
            (late,) = await emit(conn, events.PAPER_UNSAVED)  # a later id, committed first
            assert late > early
            # By id alone the dispatcher would hand over `late` and never look back.
            await dispatch.deliver(db, rec, NOW)
            assert rec.seen == []
    finally:
        await other.close()
    await deliver(db, rec, NOW)
    assert rec.seen == [early, late]


async def test_a_failing_event_waits_out_a_doubling_backoff_and_holds_back_later_ones(
    conn: Session, db
):
    rec = Recorder()
    await start(db, rec)
    a, b, c = await emit(conn, *[events.PAPER_SAVED] * 3)
    rec.fail = {b}
    await deliver(db, rec, NOW)
    cursor = await in_write(conn, outbox.cursor, rec.name)
    assert rec.seen == [a]
    assert (cursor.position.id, cursor.failing, cursor.failures) == (a, b, 1)
    assert cursor.retry_at == NOW + timedelta(seconds=30)
    assert "cannot handle" in cursor.last_error

    # Not before the backoff is over; then it fails again and waits twice as long.
    assert await deliver(db, rec, NOW + timedelta(seconds=10)) == 0
    await deliver(db, rec, NOW + timedelta(seconds=31))
    cursor = await in_write(conn, outbox.cursor, rec.name)
    assert cursor.failures == 2
    assert cursor.retry_at == NOW + timedelta(seconds=31 + 60)

    # Once it works, the rest follow; the error is kept for the record.
    rec.fail = set()
    await deliver(db, rec, NOW + timedelta(hours=1))
    cursor = await in_write(conn, outbox.cursor, rec.name)
    assert rec.seen == [a, b, c]
    assert (cursor.position.id, cursor.failing, cursor.failures) == (c, None, 0)
    assert cursor.retry_at is None and cursor.last_error


async def test_the_backoff_runs_from_when_the_event_failed(conn: Session, db):
    rec = Recorder()
    await start(db, rec)
    (a,) = await emit(conn, events.PAPER_SAVED)
    rec.fail = {a}
    rec.before_failing = lambda: asyncio.sleep(0.3)  # a slow handle
    began = datetime.now(UTC)
    await deliver(db, rec)
    cursor = await in_write(conn, outbox.cursor, rec.name)
    assert cursor.failed_at >= began + timedelta(seconds=0.3)
    assert cursor.retry_at == cursor.failed_at + timedelta(seconds=30)


async def test_backoff_is_capped_at_an_hour():
    assert dispatch.backoff(1) == timedelta(seconds=30)
    assert dispatch.backoff(3) == timedelta(seconds=120)
    assert dispatch.backoff(30) == timedelta(hours=1)


async def test_the_owner_can_retry_at_once_or_skip_the_event(conn: Session, db):
    rec = Recorder()
    await start(db, rec)
    a, b, c = await emit(conn, *[events.PAPER_SAVED] * 3)
    rec.fail = {a}
    await deliver(db, rec, NOW)

    # Retry: tried on the next pass, backoff or not.
    assert await in_write(conn, outbox.retry, rec.name)
    rec.fail = {a, b}
    await deliver(db, rec, NOW)
    assert rec.seen == []
    assert (await in_write(conn, outbox.cursor, rec.name)).failures == 2

    # Skip: the cursor goes past it and the rest follow.
    assert await in_write(conn, outbox.skip, rec.name) == a
    rec.fail = set()
    await deliver(db, rec, NOW)
    cursor = await in_write(conn, outbox.cursor, rec.name)
    assert rec.seen == [b, c]
    assert (cursor.skipped, cursor.failing) == (1, None)
    # Nothing failing: nothing to retry or skip.
    assert not await in_write(conn, outbox.retry, rec.name)
    assert await in_write(conn, outbox.skip, rec.name) is None


async def test_a_skip_during_a_retry_that_then_fails_stays_skipped(conn: Session, db):
    rec = Recorder()
    await start(db, rec)
    a, b = await emit(conn, events.PAPER_SAVED, events.PAPER_SAVED)
    rec.fail = {a}
    await deliver(db, rec, NOW)
    owner = await sessions.sibling(conn)
    try:
        # The owner skips `a` while the dispatcher is retrying it; the retry then fails.
        rec.before_failing = lambda: outbox.skip(staged(owner), rec.name)
        await in_write(conn, outbox.retry, rec.name)
        await deliver(db, rec, NOW)
    finally:
        await owner.close()
    cursor = await in_write(conn, outbox.cursor, rec.name)
    assert (cursor.position.id, cursor.failing, cursor.skipped) == (a, None, 1)
    rec.before_failing = None
    await deliver(db, rec, NOW)
    assert rec.seen == [b]


async def test_one_failing_integration_delays_no_other(conn: Session, db):
    broken, fine = Recorder("broken"), Recorder("fine")
    for rec in (broken, fine):
        await start(db, rec)
    ids = await emit(conn, *[events.PAPER_SAVED] * 3)
    broken.fail = set(ids)
    for rec in (broken, fine):
        await deliver(db, rec, NOW)
    assert (broken.seen, fine.seen) == ([], ids)
    lags = await in_read(conn, outbox.cursors, ["broken", "fine"])
    assert (lags["broken"].lag, lags["fine"].lag) == (3, 0)


class Writer(Recorder):
    """Keeps a table of its own: made in `setup`, and a row per event in `handle`, written with
    an event of its own in one write unit, as an integration may (docs/integrations.md)."""

    async def setup(self, db):
        async with db.write("writer: setup") as unit:
            await unit.execute("CREATE TABLE IF NOT EXISTS writer_done (event bigint PRIMARY KEY)")

    async def handle(self, db, event):
        outside_unit("the writer's network call")  # no unit is open around a hook
        async with db.write("writer: handle") as unit:
            await unit.execute("INSERT INTO writer_done VALUES (%s)", (event.id,))
            await outbox.emit(unit, events.CARD_READY, event.paper_id)
        self.seen.append(event.id)


async def test_an_integration_is_given_the_gate_and_writes_in_units_of_its_own(conn: Session, db):
    writer = Writer(types={events.PAPER_SAVED})
    await start(db, writer)
    ids = await emit(conn, events.PAPER_SAVED)
    assert await deliver(db, writer, NOW) == 1
    done = await (await conn.execute("SELECT event FROM writer_done")).fetchall()
    assert [r["event"] for r in done] == writer.seen == ids
    emitted = await (await conn.execute("SELECT type FROM outbox WHERE id > %s", ids)).fetchall()
    assert [r["type"] for r in emitted] == [events.CARD_READY]


# -- deliveries as work items (docs/plans/a2-work-gate.md, P2) -------------------------------------


def delivering(db, *integrations) -> tuple[WorkGate, dict]:
    """A Work gate on `db`, and the delivery Worker for `integrations`, as `serve` runs them."""
    return WorkGate(db, poll=60), {outbox.DELIVERY: DeliveryWorker(db, list(integrations))}


def served(gate: WorkGate, workers: dict):
    """`serving` the delivery Worker for the keys it loads, as `serve` gives them."""
    return serving(gate, workers, keys={outbox.DELIVERY: workers[outbox.DELIVERY].keys})


async def turns(conn: Session, name: str) -> list[dict]:
    return await items_of(conn, outbox.DELIVERY, name)


async def test_each_event_wakes_its_integrations_delivery_as_it_commits(conn: Session, db):
    rec = Recorder()
    gate, workers = delivering(db, rec)
    await workers[outbox.DELIVERY].sweep()  # as `serve` starts: set up, and its cursor made
    async with served(gate, workers):
        await until(lambda: _ended(conn, rec.name), "the first turn ends")
        ids = await emit(conn, events.PAPER_SAVED, events.PAPER_UNSAVED)

        async def delivered() -> bool:  # and the turn that handed them over has ended
            at = (await in_write(conn, outbox.cursor, rec.name)).position.id
            return (
                at == ids[-1]
                and len(await turns(conn, rec.name)) > 1
                and await _ended(conn, rec.name)
            )

        await until(delivered, "the events are delivered, and the turn ends")
    assert rec.seen == ids
    found = await turns(conn, rec.name)
    assert all(t["admission"] == f"deliver:{rec.name}" for t in found)
    assert {t["state"] for t in found} == {"ended"}  # nothing left: no turn waits


async def _ended(conn: Session, name: str) -> bool:
    found = await turns(conn, name)
    return bool(found) and {t["state"] for t in found} == {"ended"}


async def _seen(rec: Recorder, ids: list[int]) -> bool:
    return rec.seen == ids


async def test_a_turn_that_leaves_events_arranges_the_next(conn: Session, db, monkeypatch):
    monkeypatch.setattr(dispatch, "BATCH", 2)
    rec = Recorder()
    await start(db, rec)
    ids = await emit(conn, *[events.PAPER_SAVED] * 5)  # waiting before any turn runs
    await settled(db)
    gate, workers = delivering(db, rec)
    async with served(gate, workers):
        await until(lambda: _seen(rec, ids), "every event is delivered")
        await until(lambda: _ended(conn, rec.name), "the last turn ends")
    # A batch a turn, each turn its own record: the first was woken by the events, the next
    # two arranged by the turn before.
    assert [t["outcome"] for t in await turns(conn, rec.name)] == ["done"] * 3


async def test_a_failed_delivery_waits_out_its_backoff_which_new_events_do_not_cut_short(
    conn: Session, db
):
    rec = Recorder()
    await start(db, rec)
    gate, workers = delivering(db, rec)
    async with served(gate, workers):
        rec.fail_all = True  # before the event commits, so no turn can deliver it first
        (a,) = await emit(conn, events.PAPER_SAVED)

        async def failing() -> bool:
            return (await in_write(conn, outbox.cursor, rec.name)).failing == a

        await until(failing, "the event fails")
        await until(lambda: _waits(conn, rec.name), "the next turn waits")
        cursor = await in_write(conn, outbox.cursor, rec.name)
        (waiting,) = [t for t in await turns(conn, rec.name) if t["state"] == "pending"]
        assert waiting["not_before"] == cursor.retry_at
        (b,) = await emit(conn, events.PAPER_SAVED)  # the backoff stands
        (still,) = [t for t in await turns(conn, rec.name) if t["state"] == "pending"]
        assert (still["id"], still["not_before"]) == (waiting["id"], cursor.retry_at)
        # The owner's retry runs the turn now.
        rec.fail_all = False
        assert await in_write(conn, outbox.retry, rec.name)
        await until(lambda: _seen(rec, [a, b]), "both are delivered")


async def _waits(conn: Session, name: str) -> bool:
    return any(t["state"] == "pending" for t in await turns(conn, name))


class Dropping(Recorder):
    """Loses the database the first time it handles an event."""

    def __init__(self):
        super().__init__("dropping")
        self.dropped = False

    async def handle(self, db, event):
        if not self.dropped:
            self.dropped = True
            raise psycopg.OperationalError("the connection dropped")
        await super().handle(db, event)


async def test_a_lost_database_in_a_delivery_is_no_failure_and_its_turn_is_tried_again(
    conn: Session, db
):
    rec = Dropping()
    await start(db, rec)
    gate, workers = delivering(db, rec)
    async with served(gate, workers):
        ids = await emit(conn, events.PAPER_SAVED)
        await until(lambda: _seen(rec, ids), "the event is delivered")
    cursor = await in_write(conn, outbox.cursor, rec.name)
    assert (cursor.failing, cursor.failures, cursor.last_error) == (None, 0, None)
    (turn, *_) = await turns(conn, rec.name)
    assert turn["attempts"] == 2  # given back, and the same item run again


async def test_a_process_takes_only_the_deliveries_of_the_integrations_it_loads(conn: Session, db):
    gone, rec = Recorder("gone"), Recorder()
    for each in (gone, rec):
        await start(db, each)
    gate, workers = delivering(db, rec)  # `gone` is configured in another process, or none
    # What a turn's item holds reads back in any session's zone (one parked at the end of time
    # read as the year 10000 in Tokyo, and broke the emitting write).
    await conn.execute("SET TimeZone = 'Asia/Tokyo'")
    async with served(gate, workers):
        ids = await emit(conn, events.PAPER_SAVED)
        await until(lambda: _seen(rec, ids), "the loaded one gets it")
        ids += await emit(conn, events.PAPER_SAVED, events.PAPER_SAVED)
        await until(lambda: _seen(rec, ids), "the loaded one gets them")
        await until(lambda: _ended(conn, rec.name), "its last turn ends")
    # Never taken here: one item, waiting for a process that loads it.
    assert [(t["state"], t["attempts"]) for t in await turns(conn, gone.name)] == [("pending", 0)]
    gate, workers = delivering(db, gone)  # a process that loads it starts
    await workers[outbox.DELIVERY].sweep()
    async with served(gate, workers):
        await until(lambda: _seen(gone, ids), "it delivers what waited")


async def test_an_emitter_that_looked_before_the_first_cursor_was_made_strands_nothing(
    conn: Session, db
):
    rec = Recorder("new")
    gate, workers = delivering(db, rec)
    other = await sessions.sibling(conn)

    async def waits_for_the_emitter() -> bool:
        found = await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE relation = 'outbox'::regclass "
            "AND mode = 'ShareLock' AND NOT granted) AS waits"
        )
        return (await found.fetchone())["waits"]

    async def caught_up() -> bool:
        cursors = await in_read(conn, outbox.cursors, [rec.name])
        return rec.name in cursors and cursors[rec.name].lag == 0 and await _ended(conn, rec.name)

    try:
        async with served(gate, workers):
            async with other.transaction():
                unit = staged(other)
                await outbox.emit(unit, events.PAPER_SAVED, "2610.00001")
                await outbox.wake_all(unit)  # as its commit does: `new` has no cursor yet
                await workers[outbox.DELIVERY].sweep()  # `new` is enabled: its first turn
                await until(waits_for_the_emitter, "the first cursor waits for the emitter")
            await until(caught_up, "no event is left behind the cursor")
    finally:
        await other.close()


class Picky(Recorder):
    """An integration whose filter raises while `unsure`."""

    def __init__(self):
        super().__init__("picky")
        self.unsure = True

    def wants(self, event):
        if self.unsure:
            raise RuntimeError("cannot tell")
        return super().wants(event)


async def test_a_filter_that_raises_fails_the_event_for_the_owner_to_retry(conn: Session, db):
    rec = Picky()
    await start(db, rec)
    (a,) = await emit(conn, events.PAPER_SAVED)
    assert await deliver(db, rec, NOW) == 0
    cursor = await in_write(conn, outbox.cursor, rec.name)
    assert (cursor.failing, cursor.failures) == (a, 1)
    assert cursor.last_error == "RuntimeError: cannot tell"
    rec.unsure = False
    assert await in_write(conn, outbox.retry, rec.name)
    assert await deliver(db, rec, NOW) == 1 and rec.seen == [a]


class Idling(Recorder):
    """An integration with an `idle` hook, which fails the first time."""

    def __init__(self):
        super().__init__("idling")
        self.idled = 0

    async def idle(self, db):
        self.idled += 1
        if self.idled == 1:
            raise RuntimeError("the remote end is down")


async def test_an_idle_hook_is_called_while_nothing_waits_and_its_failure_changes_nothing(
    conn: Session, db, monkeypatch
):
    monkeypatch.setattr(dispatch, "IDLE_SECONDS", 0.05)
    rec = Idling()
    gate, workers = delivering(db, rec)
    await workers[outbox.DELIVERY].sweep()

    async def idled() -> bool:
        return rec.idled >= 3

    async with served(gate, workers):
        await until(idled, "it idles again and again")
        ids = await emit(conn, events.PAPER_SAVED)
        await until(lambda: _seen(rec, ids), "events are still delivered")
    found = await turns(conn, rec.name)
    assert {t["outcome"] for t in found if t["state"] == "ended"} == {"done"}
    assert (await in_write(conn, outbox.cursor, rec.name)).failing is None


class Holding(Recorder):
    """Holds the event it is handed until `release` is set, then fails it."""

    def __init__(self):
        super().__init__("holding")
        self.handling, self.release = asyncio.Event(), asyncio.Event()

    async def handle(self, db, event):
        self.handling.set()
        await self.release.wait()
        raise RuntimeError(f"cannot handle {event.id}")


async def test_an_event_during_a_failing_turn_does_not_cut_its_backoff_short(conn: Session, db):
    rec = Holding()
    await start(db, rec)
    gate, workers = delivering(db, rec)
    async with served(gate, workers):
        await emit(conn, events.PAPER_SAVED)
        await asyncio.wait_for(rec.handling.wait(), 10)
        await emit(conn, events.PAPER_SAVED)  # wakes the turn while it runs
        rec.release.set()
        await until(lambda: _waits(conn, rec.name), "the next turn waits")
        await until(lambda: _ended_before(conn, rec.name), "the failing turn ends")
    cursor = await in_write(conn, outbox.cursor, rec.name)
    found = await turns(conn, rec.name)
    # One turn, which failed; the next waits out the backoff, with no turn run in between.
    assert [(t["state"], t["outcome"]) for t in found] == [("ended", "done"), ("pending", None)]
    assert found[1]["not_before"] == cursor.retry_at


async def _ended_before(conn: Session, name: str) -> bool:
    return any(t["state"] == "ended" for t in await turns(conn, name))


async def test_the_app_delivers_only_to_what_reacts_to_events_and_closes_every_integration(
    monkeypatch,
):
    deep = Deep(DeepClient("http://deep.test"))
    recorder = Recorder()
    closed = []

    async def aclose():
        closed.append(recorder.name)

    recorder.aclose = aclose
    served, given = {}, {}

    async def run(gate, workers, *, keys=None):
        served.update(workers)
        given.update(keys or {})
        await asyncio.Event().wait()

    async def sweep(worker):
        pass  # no database here

    monkeypatch.setattr(WorkGate, "run", run)
    monkeypatch.setattr(DeliveryWorker, "sweep", sweep)
    app = create_app("postgresql://unused", "t", integrations=[deep, recorder])
    app.state.db = contextlib.nullcontext()  # no database: the gate the lifespan opens
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
    assert list(served) == [outbox.DELIVERY]
    assert list(served[outbox.DELIVERY].loaded.values()) == [recorder]
    assert given == {outbox.DELIVERY: [recorder.name]}  # its own integrations' turns alone
    assert deep.client.http.is_closed and closed == ["recorder"]
