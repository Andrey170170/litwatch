"""The Database gate (docs/plans/a1-database-gate.md, V1 and V2's gate part): units, the order
of their locks, the nesting check, the session locks, the bridge. On real Postgres. Where a
test needs a lock held elsewhere, today's own lock code holds it: the gate and the code not yet
moved onto it must exclude each other."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from pydantic import BaseModel

from litwatch.config import Models, RoleConfig
from litwatch.gates import database
from litwatch.gates.database import (
    STRICT_ENV,
    BridgeError,
    BusyError,
    DatabaseGate,
    LateLockError,
    NestingError,
    PoolTimeoutError,
    WriteUnit,
    outside_unit,
)
from litwatch.jobs import lock as jobs_lock
from litwatch.llm import Gateway, Request
from litwatch.llm.types import DecisionRequest, Predicate
from litwatch.store import fulltext as fulltext_store
from litwatch.store import jobs as jobs_store
from litwatch.store import outbox
from litwatch.store import taxonomy as taxonomy_store
from tests.fakes import BatchingProvider, ScriptedProvider

LOG = "litwatch.gates.database"
WAIT = 10.0  # how long a test waits on something that must happen; a deadlock ends it sooner

# Today's keys, written out: the gate's registry is checked against the locks Postgres holds.
TAXONOMY = (0, 7213913, 1)
REPLAY = (0, 7213917, 1)
RUN = (0, 7213911, 1)
DEEP_WORKER = (0, 7213915, 1)


@pytest.fixture
async def probe(conn):
    """A table to read and write."""
    await conn.execute("CREATE TABLE probe (id int PRIMARY KEY)")


@pytest.fixture
def papers():
    """Paper ids no other run on the cluster locks, in the order of their letters."""
    run = uuid4().hex
    return lambda *letters: [f"{run}.{letter}" for letter in letters]


def logged(caplog) -> list[logging.LogRecord]:
    """The gate's own log records."""
    return [r for r in caplog.records if r.name == LOG]


async def count(unit) -> int:
    return (await (await unit.execute("SELECT count(*) AS n FROM probe")).fetchone())["n"]


async def keyed(conn, key: int, arg: str | int) -> tuple[int, int, int]:
    """A two-key lock as `pg_locks` shows it; a text argument is hashed as Postgres does."""
    if isinstance(arg, int):
        return (key, arg, 2)
    row = await (
        await conn.execute("SELECT (hashtext(%s)::bigint + 4294967296) %% 4294967296 AS h", (arg,))
    ).fetchone()
    return (key, row["h"], 2)


async def paper_key(conn, paper_id: str) -> tuple[int, int, int]:
    return await keyed(conn, 7213912, paper_id)


async def hold_paper(conn, paper_id: str) -> None:
    """Take the paper's lock in `conn`'s open transaction, by its key written out: a holder the
    gate does not know."""
    await conn.execute("SELECT pg_advisory_xact_lock(7213912, hashtext(%s))", (paper_id,))


ADVISORY = (
    "SELECT l.pid, l.classid::bigint AS classid, l.objid::bigint AS objid, l.objsubid, l.mode, "
    "l.granted FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
    "WHERE d.datname = current_database() AND l.locktype = 'advisory'"
)


async def waiters(conn, key: tuple[int, int, int], count: int = 1) -> list[int]:
    """The backends waiting on the lock `key`, once `count` of them do."""
    query = ADVISORY + " AND NOT l.granted AND (l.classid, l.objid, l.objsubid) = (%s, %s, %s)"
    async with asyncio.timeout(WAIT):
        while True:
            rows = await (await conn.execute(query, key)).fetchall()
            if len(rows) >= count:
                return [r["pid"] for r in rows]
            await asyncio.sleep(0.02)


async def granted(conn, pid: int) -> dict[tuple[int, int, int], str]:
    """The advisory locks backend `pid` holds, with their modes."""
    rows = await (await conn.execute(ADVISORY + " AND l.granted AND l.pid = %s", (pid,))).fetchall()
    return {(r["classid"], r["objid"], r["objsubid"]): r["mode"] for r in rows}


async def backend(unit) -> int:
    return (await (await unit.execute("SELECT pg_backend_pid() AS pid")).fetchone())["pid"]


class Held:
    """A unit opened in a task of its own and kept open until `leave`: `entered` is set once
    its locks are taken, with what it held then."""

    def __init__(self, unit_context) -> None:
        self.entered, self._leave = asyncio.Event(), asyncio.Event()
        self.unit: WriteUnit | None = None
        self.locks: dict[tuple[int, int, int], str] = {}
        self.task = asyncio.create_task(self._hold(unit_context))

    async def _hold(self, unit_context) -> None:
        async with unit_context as unit:
            self.unit = unit
            self.locks = await granted(unit, await backend(unit))
            self.entered.set()
            await self._leave.wait()

    async def opened(self) -> None:
        """Wait until the unit is open; its failure to open is raised here."""
        entered = asyncio.create_task(self.entered.wait())
        async with asyncio.timeout(WAIT):
            await asyncio.wait({entered, self.task}, return_when=asyncio.FIRST_COMPLETED)
        entered.cancel()
        if self.task.done():
            self.task.result()

    async def leave(self) -> None:
        self._leave.set()
        async with asyncio.timeout(WAIT):
            await self.task


# -- V1: units -------------------------------------------------------------------------------------


async def test_a_read_unit_reads_one_snapshot(db, conn, probe):
    async with db.read("snapshot") as unit:
        assert await count(unit) == 0
        await conn.execute("INSERT INTO probe VALUES (1)")  # committed, on another connection
        assert await count(unit) == 0
    async with db.read("the next one") as unit:
        assert await count(unit) == 1


async def test_a_read_unit_refuses_a_write(db, conn, probe):
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        async with db.read("reader") as unit:
            await unit.execute("INSERT INTO probe VALUES (1)")
    assert await count(conn) == 0


async def test_a_write_unit_commits_when_its_block_ends(db, conn, probe):
    async with db.write("writer") as unit:
        await unit.execute("INSERT INTO probe VALUES (1)")
        assert await count(conn) == 0  # not before
    assert await count(conn) == 1


async def test_a_write_unit_rolls_back_when_its_block_raises(db, conn, probe):
    with pytest.raises(LookupError):
        async with db.write("writer") as unit:
            await unit.execute("INSERT INTO probe VALUES (1)")
            raise LookupError("the block failed")
    assert await count(conn) == 0


async def test_a_unit_speaks_the_connections_language(db, conn, probe):
    async with db.write("writer") as unit:
        await unit.executemany("INSERT INTO probe VALUES (%s)", [(1,), (2,)])
        async with unit.copy("COPY probe (id) FROM STDIN") as copy:
            await copy.write_row((3,))
        async with unit.cursor() as cur:
            await cur.execute("SELECT id FROM probe ORDER BY id")
            assert await cur.fetchall() == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert await count(conn) == 3


async def test_a_closed_unit_runs_nothing(db):
    async with db.read("reader") as unit:
        pass
    with pytest.raises(RuntimeError, match="'reader' is closed"):
        await unit.execute("SELECT 1")


@pytest.mark.parametrize(("outer", "inner"), [("write", "read"), ("read", "write")])
async def test_a_unit_inside_a_unit_raises_when_strict(db, outer, inner):
    async with getattr(db, outer)("the outer"):
        with pytest.raises(NestingError, match="'the inner' while unit 'the outer' is open"):
            async with getattr(db, inner)("the inner"):
                pass
    async with getattr(db, inner)("the inner"):  # after it, a unit opens again
        pass


async def test_a_unit_inside_a_unit_is_logged_when_lenient(db, conn, probe, monkeypatch, caplog):
    monkeypatch.delenv(STRICT_ENV)
    with caplog.at_level(logging.ERROR, logger=LOG):
        async with db.write("the outer"):
            async with db.write("the inner") as unit:
                await unit.execute("INSERT INTO probe VALUES (1)")
            assert [r.getMessage() for r in logged(caplog)] == [
                "gate nesting: opening unit 'the inner' while unit 'the outer' is open"
            ]
            outside_unit("a fetch")  # the outer one is the open unit again
    records = logged(caplog)
    assert records[-1].getMessage() == "gate nesting: a fetch while unit 'the outer' is open"
    assert len(records) == 2 and all(r.levelno == logging.ERROR for r in records)
    assert await count(conn) == 1  # and work carried on


async def test_a_task_started_in_a_unit_is_inside_it_while_it_is_open(db):
    checked, closed = asyncio.Event(), asyncio.Event()

    async def child() -> None:
        with pytest.raises(NestingError, match="the child's fetch while unit 'the parent' is open"):
            outside_unit("the child's fetch")
        checked.set()
        await closed.wait()
        outside_unit("the child's fetch")  # the parent's unit closed: nothing is held

    async with db.read("the parent"):
        task = asyncio.create_task(child())
        await checked.wait()
    closed.set()
    await task


# -- V1: the model gateway -------------------------------------------------------------------------


class Answer(BaseModel):
    yes: bool


HAIKU = RoleConfig(provider="anthropic", model="claude-haiku-5-5", effort="low")
REQUEST = Request(system="rubric", user="paper", output=Answer)
DECISION = DecisionRequest("Title: a paper", (Predicate("circuits", "Is it about circuits?"),))


def model_gateway(provider) -> Gateway:
    models = Models(writer=HAIKU, classify=HAIKU)
    return Gateway(models, {"anthropic": provider}, retry_delays=(0.0,))


CALLS = {
    "ask": lambda gw: gw.ask("writer", REQUEST),
    "complete": lambda gw: gw.complete("writer", HAIKU, REQUEST),
    "decide": lambda gw: gw.decide("classify", DECISION),
    "refused_throughout": lambda gw: gw.refused_throughout("writer", REQUEST),
}


@pytest.mark.parametrize("call", CALLS)
async def test_the_model_gateway_called_inside_a_unit_raises_when_strict(db, call):
    provider = ScriptedProvider("anthropic", lambda role, request: json.dumps({"yes": True}))
    gw = model_gateway(provider)
    async with db.read("the reader"):
        with pytest.raises(NestingError, match="role '.*' while unit 'the reader' is open"):
            await CALLS[call](gw)
    assert provider.calls == []  # refused before the provider was asked
    assert (await gw.ask("writer", REQUEST)).value == Answer(yes=True)  # outside one, it answers


async def test_a_batched_model_call_inside_a_unit_raises_when_strict(db):
    provider = BatchingProvider("anthropic", lambda role, request: json.dumps({"yes": True}))
    gw = model_gateway(provider)
    gw.batch_until(datetime.now(UTC) + timedelta(hours=1))
    try:
        async with db.read("the reader"):
            with pytest.raises(NestingError):
                await gw.ask("writer", REQUEST)
        assert provider.batches == {} and provider.calls == []  # nothing waits to be sent
    finally:
        await gw.close()


async def test_the_model_gateway_inside_a_unit_is_logged_when_lenient(db, monkeypatch, caplog):
    monkeypatch.delenv(STRICT_ENV)
    gw = model_gateway(ScriptedProvider("anthropic", lambda role, request: '{"yes": true}'))
    with caplog.at_level(logging.ERROR, logger=LOG):
        async with db.read("the reader"):
            assert (await gw.ask("writer", REQUEST)).value == Answer(yes=True)
    assert [(r.levelno, r.getMessage()) for r in logged(caplog)] == [
        (
            logging.ERROR,
            "gate nesting: a model call for role 'writer' while unit 'the reader' is open",
        )
    ]


# -- V2: locks -------------------------------------------------------------------------------------


async def test_locks_are_taken_taxonomy_then_papers_by_id_then_the_rest(db, conn, papers):
    a, b, c = papers("a", "b", "c")
    topic = uuid4().hex
    key_a, key_b, key_c = [await paper_key(conn, p) for p in (a, b, c)]
    rerun = await keyed(conn, 7213916, topic)
    async with conn.transaction():
        await hold_paper(conn, b)
        held = Held(
            db.write("u", locks=["replay", ("rerun", topic)], papers=[c, b, a], taxonomy="shared"),
        )
        (pid,) = await waiters(conn, key_b)
        # Stopped at b: the taxonomy and a are taken, c and the others not yet.
        assert await granted(conn, pid) == {TAXONOMY: "ShareLock", key_a: "ExclusiveLock"}
    await held.opened()
    assert held.locks == {
        TAXONOMY: "ShareLock",
        key_a: "ExclusiveLock",
        key_b: "ExclusiveLock",
        key_c: "ExclusiveLock",
        rerun: "ExclusiveLock",
        REPLAY: "ExclusiveLock",
    }
    assert held.unit.papers == {a, b, c} and held.unit.locks == (("rerun", topic), "replay")
    await held.leave()
    assert await granted(conn, pid) == {}  # let go at commit


async def test_the_other_locks_are_taken_in_the_registrys_order(db, conn):
    topic = uuid4().hex
    async with conn.transaction():
        await jobs_store.open_rerun(conn, {"topic": topic})  # today's code holds the re-run lock
        held = Held(db.write("u", locks=["replay", ("rerun", topic), ("retally", 7)]))
        (pid,) = await waiters(conn, await keyed(conn, 7213916, topic))
        assert await granted(conn, pid) == {}  # the replay lock, named first, comes after it
    await held.opened()
    assert set(held.locks) == {await keyed(conn, 7213916, topic), REPLAY, (7213912, 7, 2)}
    await held.leave()


@pytest.mark.parametrize(
    ("held_today", "unit_takes", "waits"),
    [
        ("lock", "shared", True),
        ("lock", "exclusive", True),
        ("lock_shared", "exclusive", True),
        ("lock_shared", "shared", False),
    ],
)
async def test_the_taxonomy_lock_is_todays(db, conn, held_today, unit_takes, waits):
    async with conn.transaction():
        await getattr(taxonomy_store, held_today)(conn)
        held = Held(db.write("u", taxonomy=unit_takes))
        if waits:
            await waiters(conn, TAXONOMY)
            assert not held.entered.is_set()
        else:
            await held.opened()  # two shared holders, side by side
    await held.opened()
    assert held.locks[TAXONOMY] == ("ShareLock" if unit_takes == "shared" else "ExclusiveLock")
    await held.leave()


async def test_a_lock_asked_for_late_raises(db, conn, papers):
    a, b = papers("a", "b")
    for taxonomy, named in (("shared", [a]), (None, [])):
        async with db.write("late", taxonomy=taxonomy, papers=named) as unit:
            with pytest.raises(LateLockError, match="'late' asked for paper locks after it opened"):
                await unit.lock_papers([b])
            assert await paper_key(conn, b) not in await granted(conn, await backend(unit))
            assert unit.papers == set(named)


async def test_papers_may_be_added_under_the_exclusive_taxonomy_lock(db, conn, papers):
    a, b, c = papers("a", "b", "c")
    keys = {await paper_key(conn, p) for p in (a, b, c)}
    async with db.write("edit", taxonomy="exclusive", papers=[a]) as unit:
        await unit.lock_papers([c, b, a])
        pid = await backend(unit)
        assert unit.papers == {a, b, c}
        assert keys | {TAXONOMY} == set(await granted(conn, pid))
    assert await granted(conn, pid) == {}


async def test_two_units_on_overlapping_papers_named_in_reverse_order_both_finish(db, conn, papers):
    """Staged so that, taken as named, the two would deadlock: the unit naming (a, b) queues on
    `a` first, the one naming (b, a) second, holding `b` if it took `b` first. Postgres ends a
    deadlock with an error after a second, which fails the test before its timeout."""
    a, b = papers("a", "b")
    key_a = await paper_key(conn, a)
    async with conn.transaction():
        await hold_paper(conn, a)
        first = Held(db.write("first", taxonomy="shared", papers=[a, b]))
        await waiters(conn, key_a, 1)
        second = Held(db.write("second", taxonomy="shared", papers=[b, a]))
        await waiters(conn, key_a, 2)
    await first.opened()
    await first.leave()
    await second.opened()
    await second.leave()
    assert first.unit.papers == second.unit.papers == {a, b}


@pytest.mark.parametrize(
    ("named", "error"),
    [
        ({"papers": ["2609.1"]}, "names papers without the taxonomy lock"),
        ({"papers": "2609.1", "taxonomy": "shared"}, "a collection of ids"),
        ({"taxonomy": "shard"}, "shared or exclusive"),
        ({"locks": ["run"]}, "'run' is not a lock a unit takes"),
        ({"locks": ["settle"]}, "'settle' is not a lock a unit takes"),
        ({"locks": ["taxonomy"]}, "named by `taxonomy` and `papers`"),
        ({"locks": ["rerun"]}, "the rerun lock is taken for text"),
        ({"locks": [("retally", "7")]}, "the retally lock is taken for int"),
        ({"locks": [("replay", "x")]}, "the replay lock takes no argument"),
    ],
)
async def test_a_write_unit_refuses_locks_it_cannot_take(db, named, error):
    with pytest.raises((ValueError, TypeError), match=error):
        async with db.write("u", **named):
            pass


# -- V2: session locks -----------------------------------------------------------------------------


async def test_a_session_lock_held_by_one_gate_is_busy_for_another(db, conn):
    async with DatabaseGate(conn.test_dsn, conn.test_schema) as other:
        async with db.held("run") as mine:
            with pytest.raises(BusyError, match="the run lock is held by another session"):
                async with other.held("run"):
                    pass
            with pytest.raises(BusyError):  # nor twice by one gate: each has its own session
                async with db.held("run"):
                    pass
            assert await other.held_elsewhere("run")
            assert await db.held_elsewhere("run")  # another task of this process holding it counts
            assert not await db.held_elsewhere("run", mine=mine)  # the holder's own does not
            async with db.read("a unit meanwhile") as unit:  # the lock is not a unit
                assert (await backend(unit)) not in await holders(conn, RUN)
        assert not await other.held_elsewhere("run")
        async with other.held("run"):  # let go at the block's end
            assert await db.held_elsewhere("run")


async def holders(conn, key: tuple[int, int, int]) -> list[int]:
    query = ADVISORY + " AND l.granted AND (l.classid, l.objid, l.objsubid) = (%s, %s, %s)"
    return [r["pid"] for r in await (await conn.execute(query, key)).fetchall()]


async def test_a_keyed_session_lock_is_busy_only_for_its_key(db):
    async with db.held("schedule", "nightly"), db.held("schedule", "publications"):
        with pytest.raises(BusyError, match=r"the schedule \(nightly\) lock is held"):
            async with db.held("schedule", "nightly"):
                pass


@asynccontextmanager
async def todays(conn, name: str):
    """Today's own holder of a session lock, on `conn`; yields whether it got the lock."""
    try:
        if name == "run":
            async with jobs_lock.run_lock(conn):
                yield True
        elif name == "schedule":
            async with jobs_lock.job_lock(conn, "publications"):
                yield True
        elif name == "dispatch":
            async with outbox.dispatching(conn, "zotero") as locked:
                yield locked
        else:
            async with fulltext_store.deep_worker(conn) as locked:
                yield locked
    except jobs_lock.BusyError:
        yield False


SESSION = {"run": None, "schedule": "publications", "dispatch": "zotero", "deep-worker": None}


@pytest.mark.parametrize("name", SESSION)
async def test_the_session_locks_are_todays(db, conn, name):
    async with todays(conn, name) as locked:
        assert locked
        with pytest.raises(BusyError):
            async with db.held(name, SESSION[name]):
                pass
    async with db.held(name, SESSION[name]), todays(conn, name) as locked:
        assert not locked
    async with todays(conn, name) as locked:
        assert locked


async def test_only_the_listed_session_locks_are_held_across_calls(db):
    for name in ("replay", "migration", "paper", "nightly"):
        with pytest.raises(ValueError, match="not a session lock held across calls"):
            async with db.held(name):
                pass
    with pytest.raises(ValueError, match="not a session lock without an argument"):
        await db.held_elsewhere("schedule")


async def test_a_session_lock_lost_with_its_connection_is_logged(db, conn, caplog):
    with caplog.at_level(logging.ERROR, logger=LOG):
        async with db.held("deep-worker"):
            (pid,) = await holders(conn, DEEP_WORKER)
            await conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
    assert [r.getMessage() for r in logged(caplog)] == [
        "the deep-worker lock was lost while held: its connection ended"
    ]
    async with db.held("deep-worker"):  # kept to the end, it is let go without a word
        pass
    assert len(logged(caplog)) == 1


# -- the bridge, the pool, slow units --------------------------------------------------------------


async def test_the_bridge_runs_units_on_the_given_connection(conn, probe):
    db = DatabaseGate.over(conn)
    async with db.write("bridged") as unit:
        assert await backend(unit) == conn.info.backend_pid
        await unit.execute("INSERT INTO probe VALUES (1)")
    with pytest.raises(LookupError):
        async with db.write("bridged") as unit:
            await unit.execute("INSERT INTO probe VALUES (2)")
            raise LookupError("the block failed")
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        async with db.read("bridged") as unit:
            await unit.execute("INSERT INTO probe VALUES (3)")
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert await count(conn) == 1


async def test_the_bridge_refuses_a_connection_in_a_transaction(conn, probe):
    db = DatabaseGate.over(conn)
    async with conn.transaction():
        with pytest.raises(BridgeError, match="not idle"):
            DatabaseGate.over(conn)
        for unit in (db.read("r"), db.write("w")):
            with pytest.raises(BridgeError, match="not idle"):
                async with unit:
                    pass
        await conn.execute("INSERT INTO probe VALUES (1)")
        raise psycopg.Rollback
    # The refused units took no part in the caller's transaction: its rollback is whole.
    assert await count(conn) == 0


async def test_a_unit_that_gets_no_connection_fails_naming_itself(conn):
    async with DatabaseGate(conn.test_dsn, conn.test_schema, max_size=1, timeout=0.2) as db:
        holder = Held(db.write("the holder"))
        await holder.opened()
        with pytest.raises(PoolTimeoutError, match="unit 'the starved' waited 0.2s"):
            async with db.read("the starved"):
                pass
        await holder.leave()
        async with db.read("the starved"):  # the connection is back
            pass


async def test_a_slow_unit_is_logged_with_its_name_and_duration(db, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=LOG):
        async with db.read("quick"):
            pass
        assert logged(caplog) == []
        monkeypatch.setattr(database, "SLOW_UNIT_SECONDS", 0.0)
        async with db.write("slow"):
            pass
    (record,) = logged(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("slow unit: 'slow' was open for 0.")
