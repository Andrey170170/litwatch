"""The Work gate (docs/plans/a2-work-gate.md, V1): submission, keys, `not_before`, admission,
leases and their take-back, the fence in write units, heartbeats, settling, stopping, and
waking by NOTIFY or the poll. On real Postgres; where two sessions meet (the fence, a takeover,
a heartbeat, a claim), a test waits on what Postgres shows (a backend waiting on a lock) or on
durable state, under a timeout, never on time alone."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.abc import Params

from litwatch.core.work import ItemState, Outcome, WorkItem
from litwatch.gates.context import Attribution, Work, attribution, current_work, working
from litwatch.gates.database import Cursor, DatabaseGate, LeaseLostError, Query, WriteUnit
from litwatch.gates.work import SYSTEM, Done, Later, Worker, WorkGate
from litwatch.store import work as queue
from tests.sessions import Session, in_read, sibling, staged
from tests.working import WAIT, serving, until

SHORT = timedelta(seconds=1.5)  # a lease that lapses within a test
LONG = timedelta(minutes=5)  # one that does not


async def submit(db: DatabaseGate, kind: str, key: str, **kwargs) -> WorkItem:
    async with db.write("submit") as unit:
        return await queue.submit(unit, kind, key, **kwargs)


async def take(gate: WorkGate, kinds: list[str]) -> WorkItem | None:
    """The gate's next claim, leased as `run` leases it."""
    taken = await gate._take(kinds)
    return None if taken is None else taken[0]


async def item(conn: Session, item_id: int) -> WorkItem:
    found = await in_read(conn, queue.get, item_id)
    assert found is not None
    return found


def state_is(conn: Session, item_id: int, state: ItemState) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return (await item(conn, item_id)).state == state

    return check


async def passed(conn: Session, at: datetime | None) -> bool:
    """Whether the database's clock is past `at`, a lease's end."""
    assert at is not None
    row = await (await conn.execute("SELECT clock_timestamp() > %s AS ok", (at,))).fetchone()
    return row["ok"]


async def waiting_on_a_lock(conn: Session, statement: str) -> bool:
    """Whether a backend runs `statement…` and waits on a lock."""
    row = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND query LIKE %s) AS ok",
            (statement + "%",),
        )
    ).fetchone()
    return row["ok"]


@pytest.fixture
async def probe(conn: Session):
    """A table for a work item's writes."""
    await conn.execute("CREATE TABLE probe (id int PRIMARY KEY)")


async def probed(conn: Session) -> list[int]:
    return [
        r["id"] for r in await (await conn.execute("SELECT id FROM probe ORDER BY id")).fetchall()
    ]


@pytest.fixture
async def other(conn: Session) -> AsyncGenerator[Session]:
    """A second session, as another process's, to hold a row or a claim open."""
    async with await sibling(conn) as session:
        yield session


def finished(event: asyncio.Event) -> Worker:
    """A Worker that sets `event` and is done."""

    async def worker(item: WorkItem) -> None:
        event.set()

    return worker


# -- submitting ------------------------------------------------------------------------------------


async def test_a_submission_commits_with_its_unit_or_not_at_all(conn: Session, db):
    with pytest.raises(RuntimeError, match="the caller's data failed"):
        async with db.write("rolled back") as unit:
            await queue.submit(unit, "fulltext", "1")
            raise RuntimeError("the caller's data failed")
    assert (await (await conn.execute("SELECT count(*) AS n FROM work_items")).fetchone())["n"] == 0

    submitted = await submit(db, "fulltext", "1", scope={"pass": 1})
    assert await item(conn, submitted.id) == submitted
    assert submitted.state == ItemState.PENDING and submitted.scope == {"pass": 1}


async def test_an_open_key_is_the_same_item(conn: Session, db):
    gate = WorkGate(db)
    later = datetime.now(UTC) + timedelta(hours=1)
    first = await submit(db, "fulltext", "7", not_before=later)
    again = await submit(db, "fulltext", "7", not_before=later + timedelta(hours=1))
    assert again.id == first.id and again.not_before == first.not_before
    # Submitted sooner ("run it now"), the pending item is brought forward.
    sooner = await submit(db, "fulltext", "7")
    assert sooner.id == first.id and sooner.not_before < first.not_before

    taken = await take(gate, ["fulltext"])
    assert taken is not None and taken.id == first.id
    async with db.write("end") as unit:
        assert await queue.end(unit, taken, Outcome.DONE)
    assert (await submit(db, "fulltext", "7")).id != first.id  # ended, the key is free


async def test_due_items_are_taken_by_priority_then_by_how_long_they_waited(conn: Session, db):
    gate = WorkGate(db)
    past = datetime.now(UTC) - timedelta(minutes=5)
    await submit(db, "pass", "old", not_before=past, admission="a", priority=1)
    await submit(db, "pass", "urgent", admission="b")
    await submit(
        db, "pass", "newer", not_before=past + timedelta(minutes=1), admission="c", priority=1
    )
    await submit(db, "pass", "moved", not_before=past, admission="d", priority=2)
    # Submitted again more urgently (the owner's request), a pending item moves up, and keeps
    # its time; one later than it has keeps it too.
    soon = datetime.now(UTC) + timedelta(hours=1)
    moved = await submit(db, "pass", "moved", not_before=soon, priority=-1)
    assert (moved.priority, moved.not_before) == (-1, past)
    taken = [await take(gate, ["pass"]) for _ in range(4)]
    assert [t.key for t in taken if t] == ["moved", "urgent", "old", "newer"]

    # A resubmission while it runs is kept by the item that takes its key.
    running = taken[0]
    assert running is not None
    await submit(db, "pass", "moved", priority=-3)
    async with db.write("end") as unit:
        assert await queue.end(unit, running, Outcome.DONE)
    successor = await (
        await conn.execute(
            "SELECT priority FROM work_items WHERE key = 'moved' AND state = 'pending'"
        )
    ).fetchone()
    assert successor["priority"] == -3


# A resubmitted item ended done, and the item that took its key.
ENDED_THEN_NEXT = [("ended", "done", None, False), ("pending", None, None, False)]


async def records(conn: Session, key: str) -> list[tuple]:
    """Every item of the key, oldest first: its state, outcome, error and resubmission."""
    rows = await (
        await conn.execute(
            "SELECT state, outcome, error, resubmitted FROM work_items WHERE key = %s ORDER BY id",
            (key,),
        )
    ).fetchall()
    return [(r["state"], r["outcome"], r["error"], r["resubmitted"]) for r in rows]


async def test_a_resubmitted_item_that_ends_leaves_its_record_and_a_successor(conn: Session, db):
    gate = WorkGate(db)
    first = await submit(db, "fulltext", "7", scope={"pass": 7}, admission="text")
    taken = await take(gate, ["fulltext"])
    assert taken is not None
    # The owner's retry while it runs: the same item, marked.
    assert (await submit(db, "fulltext", "7")).id == first.id
    async with db.write("end") as unit:
        assert await queue.end(unit, taken, Outcome.FAILED, error="no text")
    assert await records(conn, "7") == [
        ("ended", "failed", "no text", False),
        ("pending", None, None, False),
    ]
    successor = await take(gate, ["fulltext"])
    assert successor is not None and successor.id != first.id
    assert (successor.scope, successor.admission, successor.attempts) == ({"pass": 7}, "text", 1)


async def test_a_resubmission_during_a_claim_marks_the_claimed_item(conn: Session, db, other):
    first = await submit(db, "fulltext", "7")
    async with other.transaction():
        # A claim in flight: the submitter, which finds the item pending, waits for it.
        claimed = await queue.take(staged(other), ["fulltext"], LONG)
        assert claimed is not None
        claimed = await queue.stamp(staged(other), claimed, LONG)
        submitting = asyncio.create_task(submit(db, "fulltext", "7"))
        await until(lambda: waiting_on_a_lock(conn, "SELECT id, kind"), "the submitter waits")
    async with asyncio.timeout(WAIT):
        found = await submitting
    assert (found.id, found.state) == (first.id, ItemState.RUNNING)
    async with db.write("end") as unit:
        assert await queue.end(unit, claimed, Outcome.DONE)
    assert await records(conn, "7") == ENDED_THEN_NEXT


async def test_a_resubmission_open_while_the_item_settles_is_not_lost(conn: Session, db, other):
    gate = WorkGate(db)
    first = await submit(db, "fulltext", "7")
    taken = await take(gate, ["fulltext"])
    assert taken is not None
    async with other.transaction():
        # The owner's retry, not yet committed, while the item's run settles.
        assert (await queue.submit(staged(other), "fulltext", "7")).id == first.id
        settling = asyncio.create_task(
            gate._change("end", lambda u: queue.end(u, taken, Outcome.DONE))
        )
        await until(lambda: waiting_on_a_lock(conn, "SELECT 1 FROM work_items"), "settling waits")
    async with asyncio.timeout(WAIT):
        await settling
    assert await records(conn, "7") == ENDED_THEN_NEXT


async def test_a_resubmitted_item_that_runs_again_stays_one_item(conn: Session, db):
    gate = WorkGate(db, lease=SHORT)
    first = await submit(db, "fulltext", "7")
    soon = datetime.now(UTC) + timedelta(minutes=5)

    async def taken_and_resubmitted(wanted: datetime | None = None) -> WorkItem:
        taken = await take(gate, ["fulltext"])
        assert taken is not None and taken.id == first.id
        await submit(db, "fulltext", "7", not_before=wanted)
        return taken

    # It waits (`Later`): the sooner of the two times.
    taken = await taken_and_resubmitted()
    async with db.write("later") as unit:
        assert await queue.again(unit, taken, datetime.now(UTC) + timedelta(hours=1))
    assert await records(conn, "7") == [("pending", None, None, False)]
    assert (await item(conn, first.id)).not_before < soon

    # It is given back as the gate stops.
    taken = await taken_and_resubmitted()
    async with db.write("give back") as unit:
        assert await queue.again(unit, taken, live=False)
    assert await records(conn, "7") == [("pending", None, None, False)]

    # Its lease expires and it is taken back, a later wish brought forward to now.
    taken = await taken_and_resubmitted(datetime.now(UTC) + timedelta(hours=1))
    await until(lambda: passed(conn, taken.lease_until), "the lease lapsed")
    assert [t.id for t in await gate.take_back(["fulltext"])] == [first.id]
    assert await records(conn, "7") == [("pending", None, None, False)]
    assert (await item(conn, first.id)).not_before < soon
    assert (await take(gate, ["fulltext"])) is not None


async def test_a_scheduled_key_holds_after_its_item_ends(conn: Session, db):
    gate = WorkGate(db)
    # Pending, a submission at another time leaves it as it is.
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    next_fire = await submit(
        db, "nightly", "2026-11-25T02:00+00:00", not_before=tomorrow, lasting=True
    )
    again = await submit(db, "nightly", "2026-11-25T02:00+00:00", lasting=True)
    assert (again.id, again.state, again.not_before) == (next_fire.id, ItemState.PENDING, tomorrow)

    fire = await submit(db, "nightly", "2026-11-24T02:00+00:00", lasting=True)
    taken = await take(gate, ["nightly"])
    assert taken is not None
    assert (await submit(db, "nightly", "2026-11-24T02:00+00:00", lasting=True)).id == fire.id
    async with db.write("end") as unit:
        await queue.end(unit, taken, Outcome.DONE)
    again = await submit(db, "nightly", "2026-11-24T02:00+00:00", lasting=True)
    assert again.id == fire.id and again.state == ItemState.ENDED  # not run again
    assert await take(gate, ["nightly"]) is None


class PausingUnit(WriteUnit):
    """A submitter's unit on a session of its own, which stops before its second lookup of
    the key until `resume` is set, having set `paused`."""

    def __init__(self, conn: Session) -> None:
        super().__init__("submitter", conn, None)
        self.lookups = 0
        self.paused, self.resume = asyncio.Event(), asyncio.Event()

    async def execute(
        self,
        query: Query,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool = False,
    ) -> Cursor:
        if isinstance(query, str) and query.startswith("SELECT id, kind"):
            self.lookups += 1
            if self.lookups == 2:
                self.paused.set()
                await self.resume.wait()
        return await super().execute(query, params, prepare=prepare, binary=binary)


async def test_a_submission_follows_its_key_through_every_handoff(conn: Session, db, other):
    gate = WorkGate(db)
    first = await submit(db, "fulltext", "7")
    taken = await take(gate, ["fulltext"])
    assert taken is not None
    await submit(db, "fulltext", "7")  # resubmitted: its end hands the key to a successor
    async with await sibling(conn) as session:
        submitter = PausingUnit(session)

        async def submitting() -> WorkItem:
            async with session.transaction():
                return await queue.submit(submitter, "fulltext", "7")

        async with other.transaction():
            # The first handoff: the item ends, and its successor takes the key, as the
            # submitter waits for the item's row.
            assert await queue.end(staged(other), taken, Outcome.DONE)
            found = asyncio.create_task(submitting())
            await until(lambda: waiting_on_a_lock(conn, "SELECT id, kind"), "the lookup waits")
        async with asyncio.timeout(WAIT):
            await submitter.paused.wait()  # its insert met the successor
        async with other.transaction():
            # The second: the successor is claimed, resubmitted and ends, as the submitter
            # waits for its row in turn.
            successor = await queue.take(staged(other), ["fulltext"], LONG)
            assert successor is not None and successor.id != first.id
            successor = await queue.stamp(staged(other), successor, LONG)
            await queue.submit(staged(other), "fulltext", "7")
            assert await queue.end(staged(other), successor, Outcome.DONE)
            submitter.resume.set()
            await until(lambda: waiting_on_a_lock(conn, "SELECT id, kind"), "it waits again")
        async with asyncio.timeout(WAIT):
            last = await found
    assert last.state == ItemState.PENDING and last.id not in (first.id, successor.id)
    assert await records(conn, "7") == [ENDED_THEN_NEXT[0]] * 2 + [ENDED_THEN_NEXT[1]]


# -- admission -------------------------------------------------------------------------------------


async def test_items_wait_for_their_not_before(conn: Session, db):
    gate = WorkGate(db, poll=60)
    await submit(db, "fulltext", "later", not_before=datetime.now(UTC) + timedelta(hours=1))
    assert await take(gate, ["fulltext"]) is None

    # A gate asleep wakes when the next `not_before` comes, with no notification or poll.
    async with serving(gate, {"soon": finished(asyncio.Event())}):
        soon = await submit(db, "soon", "1", not_before=datetime.now(UTC) + timedelta(seconds=1))
        # The submission's own notification wakes the gate early; it then sleeps to the time.
        await until(state_is(conn, soon.id, ItemState.ENDED), "the item ran")
    row = await (
        await conn.execute(
            "SELECT started_at >= not_before AS ok FROM work_items WHERE id = %s", (soon.id,)
        )
    ).fetchone()
    assert row["ok"]  # claimed no earlier than its time


async def test_admission_is_one_per_kind(conn: Session, db):
    gate = WorkGate(db)
    await submit(db, "fulltext", "1")
    await submit(db, "fulltext", "2")
    await submit(db, "deep", "1")
    first = await take(gate, ["fulltext"])
    assert first is not None and first.key == "1"
    assert await take(gate, ["fulltext"]) is None
    assert (await take(gate, ["fulltext", "deep"])) is not None  # another kind runs beside it


async def test_admission_is_one_per_integration_for_deliveries(conn: Session, db):
    gate = WorkGate(db)
    await submit(db, "deliver", "webhook:1", admission="deliver:webhook")
    await submit(db, "deliver", "webhook:2", admission="deliver:webhook")
    await submit(db, "deliver", "zotero:1", admission="deliver:zotero")
    taken = [await take(gate, ["deliver"]) for _ in range(3)]
    assert [t.key if t else None for t in taken] == ["webhook:1", "zotero:1", None]


async def test_two_processes_claiming_for_one_admission_get_one(conn: Session, db, other):
    gate = WorkGate(db)
    await submit(db, "deliver", "webhook:1", admission="deliver:webhook")
    await submit(db, "deliver", "webhook:2", admission="deliver:webhook")
    async with other.transaction():
        # Another process's claim, made and not yet committed.
        mine = await queue.take(staged(other), ["deliver"], LONG)
        assert mine is not None and mine.key == "webhook:1"
        peer = asyncio.create_task(take(gate, ["deliver"]))
        await until(lambda: waiting_on_a_lock(conn, "\n            WITH next"), "the peer waits")
    async with asyncio.timeout(WAIT):
        assert await peer is None
    running = await (
        await conn.execute("SELECT key FROM work_items WHERE state = 'running'")
    ).fetchall()
    assert [r["key"] for r in running] == ["webhook:1"]


async def test_a_claim_kept_waiting_by_a_peer_starts_its_lease_when_it_wins(
    conn: Session, db, other
):
    gate = WorkGate(db, lease=SHORT)
    await submit(db, "deliver", "webhook:1", admission="deliver:webhook")
    await submit(db, "deliver", "webhook:2", admission="deliver:webhook")
    before = (await (await conn.execute("SELECT clock_timestamp() AS at")).fetchone())["at"]
    async with other.transaction():
        # A peer's claim, held open, then rolled back: the waiting claim wins after it.
        assert await queue.take(staged(other), ["deliver"], LONG) is not None
        peer = asyncio.create_task(take(gate, ["deliver"]))
        await until(lambda: waiting_on_a_lock(conn, "\n            WITH next"), "the claim waits")
        await until(lambda: passed(conn, before + SHORT), "a lease from before the wait lapsed")
        raise psycopg.Rollback()
    async with asyncio.timeout(WAIT):
        taken = await peer
    assert taken is not None and taken.key == "webhook:2"
    assert not await passed(conn, taken.lease_until)  # live as it starts


# -- leases ----------------------------------------------------------------------------------------


async def test_an_expired_lease_is_taken_back_and_the_item_tried_again(conn: Session, db):
    dead = WorkGate(db, lease=SHORT)
    submitted = await submit(db, "fulltext", "1")
    assert await take(dead, ["fulltext"]) is not None  # and its process dies

    seen: list[int] = []

    async def worker(item: WorkItem) -> Done:
        seen.append(item.attempts)
        return Done()

    async with serving(WorkGate(db, lease=SHORT, poll=60), {"fulltext": worker}):
        await until(state_is(conn, submitted.id, ItemState.ENDED), "taken back and run")
    found = await item(conn, submitted.id)
    assert (found.outcome, found.attempts, seen) == (Outcome.DONE, 2, [2])


async def test_a_write_under_a_lost_lease_fails(conn: Session, db, probe):
    gate = WorkGate(db, lease=SHORT)
    await submit(db, "fulltext", "1")
    held = await take(gate, ["fulltext"])
    assert held is not None and held.lease_until is not None
    with working(Work(held)):
        async with db.write("held") as unit:
            await unit.execute("INSERT INTO probe VALUES (1)")

    await until(lambda: passed(conn, held.lease_until), "the lease lapsed")
    with working(Work(held)), pytest.raises(LeaseLostError):  # expired, not yet taken back
        async with db.write("lapsed") as unit:
            await unit.execute("INSERT INTO probe VALUES (2)")

    assert [t.id for t in await gate.take_back(["fulltext"])] == [held.id]
    again = await take(gate, ["fulltext"])
    assert again is not None and again.id == held.id
    with working(Work(held)), pytest.raises(LeaseLostError):  # a peer holds it now
        async with db.write("taken over") as unit:
            await unit.execute("INSERT INTO probe VALUES (3)")
    assert await probed(conn) == [1]


async def test_a_fence_kept_waiting_past_the_lease_fails(conn: Session, db, other, probe):
    gate = WorkGate(db, lease=SHORT)
    await submit(db, "fulltext", "1")
    held = await take(gate, ["fulltext"])
    assert held is not None and held.lease_until is not None
    async with other.transaction():
        await other.execute("SELECT 1 FROM work_items WHERE id = %s FOR UPDATE", (held.id,))

        async def write() -> None:
            with working(Work(held)):
                async with db.write("held") as unit:
                    await unit.execute("INSERT INTO probe VALUES (1)")

        writing = asyncio.create_task(write())
        await until(lambda: waiting_on_a_lock(conn, "SELECT 1 FROM work_items"), "the fence waits")
        await until(lambda: passed(conn, held.lease_until), "the lease lapsed")
    with pytest.raises(LeaseLostError):
        async with asyncio.timeout(WAIT):
            await writing
    assert await probed(conn) == []


async def test_a_unit_holding_an_item_keeps_it_from_a_takeover(conn: Session, db, probe):
    gate = WorkGate(db, lease=SHORT)
    await submit(db, "fulltext", "1")
    await submit(db, "deep", "1")
    held = await take(gate, ["fulltext"])
    assert held is not None and held.lease_until is not None
    inside, finish = asyncio.Event(), asyncio.Event()

    async def writing() -> None:
        with working(Work(held)):
            async with db.write("held") as unit:
                await unit.execute("INSERT INTO probe VALUES (1)")
                inside.set()
                await finish.wait()

    unit = asyncio.create_task(writing())
    try:
        async with asyncio.timeout(WAIT):
            await inside.wait()
        await until(lambda: passed(conn, held.lease_until), "the lease lapsed")
        # Not taken back while the unit holds it, and the queue goes on meanwhile.
        async with asyncio.timeout(WAIT):
            assert await gate.take_back(["fulltext", "deep"]) == []
        started = await take(gate, ["fulltext", "deep"])
        assert started is not None and started.kind == "deep"
    finally:  # a failure here must not leave the unit open while the schema is dropped
        finish.set()
        async with asyncio.timeout(WAIT):
            await unit
    assert [t.id for t in await gate.take_back(["fulltext"])] == [held.id]
    assert await probed(conn) == [1]  # the unit's write, under its lease, committed


async def test_a_heartbeat_blocked_by_a_unit_goes_through_once_it_ends(conn: Session, db):
    attempts: list[int] = []
    started = asyncio.Event()

    async def worker(item: WorkItem) -> None:
        attempts.append(item.attempts)
        started.set()
        await asyncio.Event().wait()

    submitted = await submit(db, "fulltext", "1")
    # A lease long enough to let the unit go well within it, renewed every two seconds.
    async with serving(WorkGate(db, lease=timedelta(seconds=6), poll=60), {"fulltext": worker}):
        async with asyncio.timeout(WAIT):
            await started.wait()
        held = await item(conn, submitted.id)
        assert held.lease_until is not None
        inside, finish = asyncio.Event(), asyncio.Event()

        async def writing() -> None:
            with working(Work(held)):
                async with db.write("held"):
                    inside.set()
                    await finish.wait()

        unit = asyncio.create_task(writing())
        async with asyncio.timeout(WAIT):
            await inside.wait()
        await until(lambda: waiting_on_a_lock(conn, "SELECT 1 FROM work_items"), "the beat waits")
        finish.set()
        async with asyncio.timeout(WAIT):
            await unit
        assert not await passed(conn, held.lease_until)  # let go within the lease

        leases = [held.lease_until]

        async def extended_twice() -> bool:
            now = await item(conn, submitted.id)
            assert (now.state, now.lease_token) == (ItemState.RUNNING, held.lease_token)
            assert now.lease_until is not None
            if now.lease_until != leases[-1]:
                leases.append(now.lease_until)
            return len(leases) == 3

        await until(extended_twice, "the lease renewed twice")
        assert leases == sorted(leases) and attempts == [1]  # one lease, one run, extended


async def test_a_write_waiting_on_a_paper_lock_does_not_deadlock_a_submitter(conn: Session, db):
    gate = WorkGate(db)
    submitted = await submit(db, "fulltext", "1")
    held = await take(gate, ["fulltext"])
    assert held is not None
    paper = f"{uuid4().hex}.paper"
    locked, finish = asyncio.Event(), asyncio.Event()

    async def producing() -> None:
        # Settling a paper, it asks for the item's work again.
        async with db.write("producer", taxonomy="shared", papers=[paper]) as unit:
            locked.set()
            await finish.wait()
            await queue.submit(unit, "fulltext", "1")

    async def working_on_it() -> None:
        # The item's own write, for the same paper: its fence holds the item's row.
        with working(Work(held)):
            async with db.write("worker", taxonomy="shared", papers=[paper]):
                pass

    producer = asyncio.create_task(producing())
    async with asyncio.timeout(WAIT):
        await locked.wait()
    worker = asyncio.create_task(working_on_it())
    await until(lambda: waiting_on_a_lock(conn, "SELECT pg_advisory_xact_lock"), "the worker waits")
    finish.set()
    async with asyncio.timeout(WAIT):
        await producer
        await worker
    async with db.write("end") as unit:
        assert await queue.end(unit, held, Outcome.DONE)
    assert [r[0] for r in await records(conn, submitted.key)] == ["ended", "pending"]  # asked again


async def test_a_renewal_that_fails_cancels_the_items_task(conn: Session, db, monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()
    clock = asyncio.get_running_loop().time
    lease = timedelta(seconds=3)
    failed: list[float] = []
    cancelled_at: list[float] = []

    async def worker(item: WorkItem) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled_at.append(clock())
            cancelled.set()
            raise

    async def renew_lost(*args, **kwargs) -> bool:
        failed.append(clock())
        raise psycopg.OperationalError("the connection is lost")

    monkeypatch.setattr(queue, "renew", renew_lost)
    await submit(db, "fulltext", "1")
    async with serving(WorkGate(db, lease=lease, poll=60), {"fulltext": worker}):
        async with asyncio.timeout(WAIT):
            await started.wait()
            await cancelled.wait()
    assert len(failed) == 1  # the first failure, not the lease's end
    # Cancelled at the failure, not when the lease would end, two seconds later.
    assert cancelled_at[0] - failed[0] < lease.total_seconds() / 3


async def test_a_worker_stopped_for_its_lease_finishes_its_cleanup_as_the_gate_stops(
    conn: Session, db
):
    started, cleaning, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def worker(item: WorkItem) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await asyncio.sleep(0.5)  # its cleanup awaits, and the gate stops meanwhile
            cleaned.set()
            raise

    submitted = await submit(db, "fulltext", "1")
    async with serving(WorkGate(db, lease=SHORT, poll=60), {"fulltext": worker}):
        async with asyncio.timeout(WAIT):
            await started.wait()
        # A peer takes the item over: the next heartbeat stops the Worker.
        await conn.execute(
            "UPDATE work_items SET lease_token = gen_random_uuid(), lease_until = now() + "
            "interval '1 hour' WHERE id = %s",
            (submitted.id,),
        )
        async with asyncio.timeout(WAIT):
            await cleaning.wait()
    assert cleaned.is_set()


async def test_a_stopping_gate_returns_once_a_renewal_in_flight_has_ended(
    conn: Session, db, monkeypatch
):
    gate = WorkGate(db, lease=timedelta(seconds=3), poll=60)
    renewing, unwound = asyncio.Event(), asyncio.Event()

    async def stuck(item: WorkItem) -> bool:
        # A renewal whose unit hangs, and takes a while to unwind once cancelled.
        try:
            async with gate._write("renew") as unit:
                await unit.execute("SELECT 1")
                renewing.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.5)
        finally:
            unwound.set()  # its transaction is over
        return True

    monkeypatch.setattr(gate, "renew", stuck)
    started = asyncio.Event()

    async def worker(item: WorkItem) -> None:
        started.set()
        await asyncio.Event().wait()

    await submit(db, "fulltext", "1")
    running = asyncio.create_task(gate.run({"fulltext": worker}))
    async with asyncio.timeout(WAIT):
        await started.wait()
        await renewing.wait()
    running.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        async with asyncio.timeout(WAIT):
            await running
    assert unwound.is_set()


async def test_a_failed_heartbeat_cancels_the_items_task(conn: Session, db):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def worker(item: WorkItem) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    submitted = await submit(db, "fulltext", "1")
    async with serving(WorkGate(db, lease=SHORT, poll=60), {"fulltext": worker}):
        async with asyncio.timeout(WAIT):
            await started.wait()
        # A peer takes the item over: the next heartbeat finds the lease gone.
        await conn.execute(
            "UPDATE work_items SET lease_token = gen_random_uuid(), lease_until = now() + "
            "interval '1 hour' WHERE id = %s",
            (submitted.id,),
        )
        async with asyncio.timeout(WAIT):
            await cancelled.wait()
    found = await item(conn, submitted.id)
    assert found.state == ItemState.RUNNING  # still the peer's: the loser recorded nothing


async def test_a_heartbeat_kept_waiting_past_the_lease_cancels_and_revives_nothing(
    conn: Session, db, other
):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def worker(item: WorkItem) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    submitted = await submit(db, "fulltext", "1")
    async with serving(WorkGate(db, lease=SHORT, poll=60), {"fulltext": worker}):
        async with asyncio.timeout(WAIT):
            await started.wait()
        held = await item(conn, submitted.id)
        async with other.transaction():
            # A unit holds the row: the renewal waits behind it, past the lease's end.
            await other.execute("SELECT 1 FROM work_items WHERE id = %s FOR SHARE", (held.id,))
            async with asyncio.timeout(WAIT):
                await cancelled.wait()
    # Read once the gate has stopped, which waits for the cut-off renewal to end.
    row = await (
        await conn.execute(
            "SELECT lease_token = %s AND lease_until > %s AS revived FROM work_items WHERE id = %s",
            (held.lease_token, held.lease_until, held.id),
        )
    ).fetchone()
    assert not row["revived"]


async def test_a_lost_pooled_connection_leaves_the_item_to_be_tried_again(conn: Session, db):
    async def worker(item: WorkItem) -> Done:
        if item.attempts == 1:
            async with db.write("cut off") as unit:
                await unit.execute("SELECT pg_terminate_backend(pg_backend_pid())")
        return Done()

    submitted = await submit(db, "fulltext", "1")
    async with serving(WorkGate(db, poll=60), {"fulltext": worker}):
        await until(state_is(conn, submitted.id, ItemState.ENDED), "the item tried again")
    found = await item(conn, submitted.id)
    assert (found.outcome, found.attempts) == (Outcome.DONE, 2)


def _session_ended(code: str) -> Exception:
    return psycopg.errors.lookup(code)("the session ended")


@pytest.mark.parametrize(
    ("raised", "outcome"),
    [
        (lambda: _session_ended("25P03"), None),
        (lambda: _session_ended("25P04"), None),
        (lambda: ExceptionGroup("both", [psycopg.OperationalError("lost")] * 2), None),
        (
            lambda: ExceptionGroup("mixed", [psycopg.OperationalError("lost"), ValueError("bad")]),
            Outcome.FAILED,
        ),
        (lambda: psycopg.errors.QueryCanceled("statement timeout"), Outcome.FAILED),
    ],
    ids=["idle-timeout", "transaction-timeout", "all-lost", "mixed", "statement-timeout"],
)
async def test_only_a_lost_database_tries_an_item_again(conn: Session, db, raised, outcome):
    async def worker(item: WorkItem) -> None:
        if item.attempts == 1:
            raise raised()

    submitted = await submit(db, "fulltext", "1")
    async with serving(WorkGate(db, poll=60), {"fulltext": worker}):
        await until(state_is(conn, submitted.id, ItemState.ENDED), "the item settled")
    found = await item(conn, submitted.id)
    if outcome is None:
        assert (found.outcome, found.attempts) == (Outcome.DONE, 2)
    else:
        assert (found.outcome, found.attempts) == (outcome, 1)


async def test_a_worker_that_exits_while_its_last_write_fails_leaves_its_item(
    conn: Session, db, monkeypatch
):
    real_end, failed = queue.end, asyncio.Event()

    async def end_lost(*args, **kwargs) -> bool:
        failed.set()
        raise psycopg.OperationalError("the connection is lost")

    runs: list[int] = []

    async def worker(item: WorkItem) -> Done:
        runs.append(item.attempts)
        return Done()

    submitted = await submit(db, "fulltext", "1")
    monkeypatch.setattr(queue, "end", end_lost)
    async with serving(WorkGate(db, lease=SHORT, poll=60), {"fulltext": worker}):
        async with asyncio.timeout(WAIT):
            await failed.wait()
    # The process is gone; its item still holds its lease, until it lapses.
    assert (await item(conn, submitted.id)).state == ItemState.RUNNING
    monkeypatch.setattr(queue, "end", real_end)

    async with serving(WorkGate(db, lease=SHORT, poll=60), {"fulltext": worker}):
        await until(state_is(conn, submitted.id, ItemState.ENDED), "taken back and run")
    assert runs == [1, 2]
    assert (await item(conn, submitted.id)).outcome == Outcome.DONE


# -- stopping --------------------------------------------------------------------------------------


async def test_a_stopping_gate_gives_its_items_back(conn: Session, db):
    started = asyncio.Event()

    async def worker(item: WorkItem) -> None:
        started.set()
        await asyncio.Event().wait()

    submitted = await submit(db, "fulltext", "1")
    async with serving(WorkGate(db, poll=60), {"fulltext": worker}):
        async with asyncio.timeout(WAIT):
            await started.wait()
    found = await item(conn, submitted.id)
    assert (found.state, found.attempts) == (ItemState.PENDING, 1)


async def test_a_gate_stopped_as_its_claim_commits_gives_it_back(conn: Session, db, monkeypatch):
    gate = WorkGate(db, poll=60)
    take, committed, handed = gate._take, asyncio.Event(), asyncio.Event()

    async def slow_take(kinds: list[str]) -> tuple[WorkItem, float] | None:
        claimed = await take(kinds)
        if claimed is not None:
            committed.set()
            await handed.wait()  # committed, not yet handed to the runner
        return claimed

    monkeypatch.setattr(gate, "_take", slow_take)
    ran = asyncio.Event()
    running = asyncio.create_task(gate.run({"fulltext": finished(ran)}))
    submitted = await submit(db, "fulltext", "1")
    async with asyncio.timeout(WAIT):
        await committed.wait()
    running.cancel()
    handed.set()
    with contextlib.suppress(asyncio.CancelledError):
        async with asyncio.timeout(WAIT):
            await running
    found = await item(conn, submitted.id)
    assert (found.state, found.attempts, ran.is_set()) == (ItemState.PENDING, 1, False)


async def test_a_gate_stopped_while_settling_finishes_the_settling(conn: Session, db, other):
    started, release = asyncio.Event(), asyncio.Event()

    async def worker(item: WorkItem) -> None:
        started.set()
        await release.wait()

    gate = WorkGate(db, poll=60)
    submitted = await submit(db, "fulltext", "1")
    running = asyncio.create_task(gate.run({"fulltext": worker}))
    async with asyncio.timeout(WAIT):
        await started.wait()
    async with other.transaction():
        # The item's row is held as it settles: its end waits for the row.
        await other.execute("SELECT 1 FROM work_items WHERE id = %s FOR SHARE", (submitted.id,))
        release.set()
        await until(lambda: waiting_on_a_lock(conn, "SELECT 1 FROM work_items"), "the end waits")
        running.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        async with asyncio.timeout(WAIT):
            await running
    found = await item(conn, submitted.id)
    assert (found.state, found.outcome) == (ItemState.ENDED, Outcome.DONE)


# -- what an item runs under -----------------------------------------------------------------------


async def test_an_item_runs_under_its_lease_and_attribution(conn: Session, db):
    seen: list[tuple] = []

    async def worker(item: WorkItem) -> Later | Done:
        work = current_work()
        assert work is not None
        seen.append((work.item.id, work.item.lease_token, attribution()))
        if item.attempts == 1:
            return Later(datetime.now(UTC))
        return Done()

    submitted = await submit(db, "fulltext", "1")
    async with serving(WorkGate(db, poll=60), {"fulltext": worker}):
        row = await (await conn.execute("SELECT kinds FROM workers")).fetchone()
        assert row["kinds"] == ["fulltext"]
        await until(state_is(conn, submitted.id, ItemState.ENDED), "the item run twice")
    assert [(i, a) for i, _, a in seen] == [(submitted.id, Attribution.work(submitted.id))] * 2
    assert seen[0][1] != seen[1][1]  # a lease of its own each time
    assert (await (await conn.execute("SELECT count(*) AS n FROM workers")).fetchone())["n"] == 0
    assert current_work() is None and attribution() is None


async def test_a_worker_that_waits_runs_again_no_sooner_than_it_asked(conn: Session, db):
    asked: list[datetime] = []

    async def worker(item: WorkItem) -> Later | Done:
        if item.attempts == 1:
            asked.append(datetime.now(UTC) + timedelta(seconds=1.5))
            return Later(asked[0])
        return Done()

    submitted = await submit(db, "fulltext", "1")
    async with serving(WorkGate(db, poll=60), {"fulltext": worker}):
        await until(state_is(conn, submitted.id, ItemState.ENDED), "the item run twice")
    row = await (
        await conn.execute(
            "SELECT attempts, not_before, started_at FROM work_items WHERE id = %s",
            (submitted.id,),
        )
    ).fetchone()
    assert (row["attempts"], row["not_before"]) == (2, asked[0])
    assert row["started_at"] >= asked[0]


async def test_the_gates_own_work_is_attributed_to_it(conn: Session, db, monkeypatch):
    seen: set[tuple[str, object]] = set()
    write, read = db.write, db.read

    def watched(opener):
        def open_unit(name: str, **kwargs):
            seen.add((name, attribution()))
            return opener(name, **kwargs)

        return open_unit

    monkeypatch.setattr(db, "write", watched(write))
    monkeypatch.setattr(db, "read", watched(read))
    ran = asyncio.Event()
    async with serving(WorkGate(db, poll=60), {"fulltext": finished(ran)}):
        await submit(db, "fulltext", "1")
        async with asyncio.timeout(WAIT):
            await ran.wait()
    gates = {who for name, who in seen if name.startswith("work: ")}
    assert gates == {SYSTEM}


async def test_a_raising_worker_fails_its_item(conn: Session, db):
    async def worker(item: WorkItem) -> None:
        raise ValueError("no such pass")

    submitted = await submit(db, "fulltext", "1")
    async with serving(WorkGate(db, poll=60), {"fulltext": worker}):
        await until(state_is(conn, submitted.id, ItemState.ENDED), "the item failed")
    found = await item(conn, submitted.id)
    assert (found.outcome, found.error) == (Outcome.FAILED, "ValueError: no such pass")


async def test_a_lost_database_while_registering_is_waited_out(conn: Session, db, monkeypatch):
    register, tries = queue.register, []

    async def flaky(*args, **kwargs) -> int:
        tries.append(1)
        if len(tries) == 1:
            raise psycopg.OperationalError("the connection is lost")
        return await register(*args, **kwargs)

    monkeypatch.setattr(queue, "register", flaky)
    ran = asyncio.Event()
    async with serving(WorkGate(db, poll=60), {"fulltext": finished(ran)}):
        await submit(db, "fulltext", "1")
        async with asyncio.timeout(WAIT):
            await ran.wait()
    assert len(tries) == 2


# -- waking ----------------------------------------------------------------------------------------


async def test_notify_wakes_the_gate_on_commit_not_on_rollback(conn: Session, db):
    async with db.listen(queue.CHANNEL) as notes:
        with contextlib.suppress(RuntimeError):
            async with db.write("rolled back") as unit:
                await queue.submit(unit, "rolled-back", "1")
                raise RuntimeError
        await submit(db, "committed", "1")
        async with asyncio.timeout(WAIT):
            assert await anext(notes) == "committed"

    # A gate asleep for its whole poll (`serving` waits for it to sleep) runs a committed
    # submission at once.
    ran = asyncio.Event()
    async with serving(WorkGate(db, poll=60), {"fulltext": finished(ran)}):
        await submit(db, "fulltext", "1")
        async with asyncio.timeout(WAIT):
            await ran.wait()


async def test_the_poll_takes_over_after_the_listener_drops(conn: Session, db, monkeypatch):
    listen, calls = db.listen, []

    @contextlib.asynccontextmanager
    async def first_only(channel: str):
        calls.append(channel)
        if len(calls) > 1:
            await asyncio.Event().wait()  # never back
        async with listen(channel) as notes:
            yield notes

    monkeypatch.setattr(db, "listen", first_only)
    ran = asyncio.Event()
    gate = WorkGate(db, poll=0.5)
    async with serving(gate, {"fulltext": finished(ran)}):
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND query LIKE 'LISTEN %'"
        )

        async def dropped() -> bool:
            return not gate.listening.is_set()

        await until(dropped, "the listener dropped")
        await submit(db, "fulltext", "1")
        async with asyncio.timeout(WAIT):
            await ran.wait()
