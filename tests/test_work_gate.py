"""The Work gate (docs/plans/a2-work-gate.md, V1): submission, keys, `not_before`, admission,
leases and their take-back, the fence in write units, heartbeats, and waking by NOTIFY or the
poll. On real Postgres; where two sessions meet (the fence, a takeover, a heartbeat), a test
waits on what Postgres shows (a backend waiting on a lock) or on durable state, under a
timeout, never on time alone."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from litwatch.core.work import ItemState, Outcome, WorkItem
from litwatch.gates.context import Attribution, Work, attribution, current_work, working
from litwatch.gates.database import DatabaseGate, LeaseLostError
from litwatch.gates.work import Done, Later, Worker, WorkGate
from litwatch.store import work as queue
from tests.sessions import Session, in_read

WAIT = 10.0  # how long a test waits on something that must happen
SHORT = timedelta(seconds=1.5)  # a lease that lapses within a test


async def until(check: Callable[[], Awaitable[bool]], what: str) -> None:
    """Wait until `check` holds, looking again every 50 ms, for at most `WAIT`."""
    async with asyncio.timeout(WAIT):
        while not await check():
            await asyncio.sleep(0.05)
    del what  # named for the reader of a timeout's traceback


async def submit(db: DatabaseGate, kind: str, key: str, **kwargs) -> WorkItem:
    async with db.write("submit") as unit:
        return await queue.submit(unit, kind, key, **kwargs)


async def item(conn: Session, item_id: int) -> WorkItem:
    found = await in_read(conn, queue.get, item_id)
    assert found is not None
    return found


async def lapsed(conn: Session, item_id: int) -> bool:
    row = await (
        await conn.execute(
            "SELECT lease_until < now() AS ok FROM work_items WHERE id = %s", (item_id,)
        )
    ).fetchone()
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


@contextlib.asynccontextmanager
async def serving(gate: WorkGate, workers: dict[str, Worker]) -> AsyncGenerator[asyncio.Task]:
    """`gate.run(workers)` for the block, listening before it starts; stopped at the end."""
    task = asyncio.create_task(gate.run(workers))
    try:
        async with asyncio.timeout(WAIT):
            await gate.listening.wait()
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


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

    taken = await gate.take(["fulltext"])
    assert taken is not None and taken.id == first.id
    assert (await submit(db, "fulltext", "7")).id == first.id  # running is open too
    async with db.write("end") as unit:
        assert await queue.end(unit, taken, Outcome.DONE)
    assert (await submit(db, "fulltext", "7")).id != first.id  # ended, the key is free


async def test_a_scheduled_key_holds_after_its_item_ends(conn: Session, db):
    gate = WorkGate(db)
    fire = await submit(db, "nightly", "2026-11-24T02:00+00:00", lasting=True)
    taken = await gate.take(["nightly"])
    assert taken is not None
    async with db.write("end") as unit:
        await queue.end(unit, taken, Outcome.DONE)
    again = await submit(db, "nightly", "2026-11-24T02:00+00:00", lasting=True)
    assert again.id == fire.id and again.state == ItemState.ENDED
    assert await gate.take(["nightly"]) is None


# -- admission -------------------------------------------------------------------------------------


async def test_items_wait_for_their_not_before(conn: Session, db):
    gate = WorkGate(db, poll=60)
    await submit(db, "fulltext", "later", not_before=datetime.now(UTC) + timedelta(hours=1))
    assert await gate.take(["fulltext"]) is None

    # A gate asleep wakes when the next `not_before` comes, with no notification or poll.
    ran = asyncio.Event()

    async def worker(item: WorkItem) -> None:
        ran.set()

    async with serving(gate, {"soon": worker}):
        await submit(db, "soon", "1", not_before=datetime.now(UTC) + timedelta(seconds=1))
        # The submission's own notification wakes the gate early; it then sleeps to the time.
        async with asyncio.timeout(WAIT):
            await ran.wait()


async def test_admission_is_one_per_kind(conn: Session, db):
    gate = WorkGate(db)
    await submit(db, "fulltext", "1")
    await submit(db, "fulltext", "2")
    await submit(db, "deep", "1")
    first = await gate.take(["fulltext"])
    assert first is not None and first.key == "1"
    assert await gate.take(["fulltext"]) is None
    assert (await gate.take(["fulltext", "deep"])) is not None  # another kind runs beside it


async def test_admission_is_one_per_integration_for_deliveries(conn: Session, db):
    gate = WorkGate(db)
    await submit(db, "deliver", "webhook:1", admission="deliver:webhook")
    await submit(db, "deliver", "webhook:2", admission="deliver:webhook")
    await submit(db, "deliver", "zotero:1", admission="deliver:zotero")
    taken = [await gate.take(["deliver"]) for _ in range(3)]
    assert [t.key if t else None for t in taken] == ["webhook:1", "zotero:1", None]


# -- leases ----------------------------------------------------------------------------------------


async def test_an_expired_lease_is_taken_back_and_the_item_tried_again(conn: Session, db):
    dead = WorkGate(db, lease=SHORT)
    submitted = await submit(db, "fulltext", "1")
    assert await dead.take(["fulltext"]) is not None  # and its process dies

    seen: list[int] = []

    async def worker(item: WorkItem) -> Done:
        seen.append(item.attempts)
        return Done()

    async def ended() -> bool:
        return (await item(conn, submitted.id)).state == ItemState.ENDED

    async with serving(WorkGate(db, lease=SHORT, poll=60), {"fulltext": worker}):
        await until(ended, "the item taken back and run")
    found = await item(conn, submitted.id)
    assert (found.outcome, found.attempts, seen) == (Outcome.DONE, 2, [2])


async def test_a_write_under_a_lost_lease_fails(conn: Session, db, probe):
    gate = WorkGate(db, lease=SHORT)
    await submit(db, "fulltext", "1")
    held = await gate.take(["fulltext"])
    assert held is not None
    with working(Work(held)):
        async with db.write("held") as unit:
            await unit.execute("INSERT INTO probe VALUES (1)")

    await until(lambda: lapsed(conn, held.id), "the lease lapsed")
    with working(Work(held)), pytest.raises(LeaseLostError):  # expired, not yet taken back
        async with db.write("lapsed") as unit:
            await unit.execute("INSERT INTO probe VALUES (2)")

    assert [t.id for t in await gate.take_back(["fulltext"])] == [held.id]
    again = await gate.take(["fulltext"])
    assert again is not None and again.id == held.id
    with working(Work(held)), pytest.raises(LeaseLostError):  # a peer holds it now
        async with db.write("taken over") as unit:
            await unit.execute("INSERT INTO probe VALUES (3)")
    assert await probed(conn) == [1]


async def test_a_takeover_waits_for_an_open_unit(conn: Session, db, probe):
    gate = WorkGate(db, lease=SHORT)
    await submit(db, "fulltext", "1")
    held = await gate.take(["fulltext"])
    assert held is not None
    inside, finish = asyncio.Event(), asyncio.Event()

    async def writing() -> None:
        with working(Work(held)):
            async with db.write("held") as unit:
                await unit.execute("INSERT INTO probe VALUES (1)")
                inside.set()
                await finish.wait()

    unit = asyncio.create_task(writing())
    async with asyncio.timeout(WAIT):
        await inside.wait()
    await until(lambda: lapsed(conn, held.id), "the lease lapsed")
    takeover = asyncio.create_task(gate.take_back(["fulltext"]))
    await until(lambda: waiting_on_a_lock(conn, "UPDATE work_items"), "the takeover waits")
    assert not takeover.done()
    finish.set()
    async with asyncio.timeout(WAIT):
        await unit
        assert [t.id for t in await takeover] == [held.id]
    assert await probed(conn) == [1]  # the unit's write, under its lease, committed


async def test_a_heartbeat_blocked_by_a_unit_goes_through_once_it_ends(conn: Session, db, probe):
    gate = WorkGate(db)
    await submit(db, "fulltext", "1")
    held = await gate.take(["fulltext"])
    assert held is not None
    inside, finish = asyncio.Event(), asyncio.Event()

    async def writing() -> None:
        with working(Work(held)):
            async with db.write("held"):
                inside.set()
                await finish.wait()

    unit = asyncio.create_task(writing())
    async with asyncio.timeout(WAIT):
        await inside.wait()
    renewal = asyncio.create_task(gate.renew(held))
    await until(lambda: waiting_on_a_lock(conn, "UPDATE work_items"), "the renewal waits")
    finish.set()
    async with asyncio.timeout(WAIT):
        await unit
        assert await renewal


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


async def test_a_lost_pooled_connection_leaves_the_item_to_be_tried_again(conn: Session, db):
    async def worker(item: WorkItem) -> Done:
        if item.attempts == 1:
            async with db.write("cut off") as unit:
                await unit.execute("SELECT pg_terminate_backend(pg_backend_pid())")
        return Done()

    submitted = await submit(db, "fulltext", "1")

    async def ended() -> bool:
        return (await item(conn, submitted.id)).state == ItemState.ENDED

    async with serving(WorkGate(db, poll=60), {"fulltext": worker}):
        await until(ended, "the item tried again")
    found = await item(conn, submitted.id)
    assert (found.outcome, found.attempts) == (Outcome.DONE, 2)


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

    async def ended() -> bool:
        return (await item(conn, submitted.id)).state == ItemState.ENDED

    async with serving(WorkGate(db, lease=SHORT, poll=60), {"fulltext": worker}):
        await until(ended, "the item taken back and run")
    assert runs == [1, 2]
    assert (await item(conn, submitted.id)).outcome == Outcome.DONE


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

    async def ended() -> bool:
        return (await item(conn, submitted.id)).state == ItemState.ENDED

    gate = WorkGate(db, poll=60)
    async with serving(gate, {"fulltext": worker}):
        row = await (await conn.execute("SELECT kinds FROM workers")).fetchone()
        assert row["kinds"] == ["fulltext"]
        await until(ended, "the item run twice")
    assert [(i, a) for i, _, a in seen] == [(submitted.id, Attribution.work(submitted.id))] * 2
    assert seen[0][1] != seen[1][1]  # a lease of its own each time
    assert (await (await conn.execute("SELECT count(*) AS n FROM workers")).fetchone())["n"] == 0
    assert current_work() is None and attribution() is None


async def test_a_raising_worker_fails_its_item(conn: Session, db):
    async def worker(item: WorkItem) -> None:
        raise ValueError("no such pass")

    submitted = await submit(db, "fulltext", "1")

    async def ended() -> bool:
        return (await item(conn, submitted.id)).state == ItemState.ENDED

    async with serving(WorkGate(db, poll=60), {"fulltext": worker}):
        await until(ended, "the item failed")
    found = await item(conn, submitted.id)
    assert (found.outcome, found.error) == (Outcome.FAILED, "ValueError: no such pass")


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

    # A gate asleep for its whole poll runs a committed submission at once.
    ran = asyncio.Event()

    async def worker(item: WorkItem) -> None:
        ran.set()

    async with serving(WorkGate(db, poll=60), {"fulltext": worker}):
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

    async def worker(item: WorkItem) -> None:
        ran.set()

    gate = WorkGate(db, poll=0.5)
    async with serving(gate, {"fulltext": worker}):
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
