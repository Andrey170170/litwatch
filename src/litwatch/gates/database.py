"""The Database gate (docs/specs/architecture/a1-database-gate.md): the connection pool, the
units every database access runs in, and every advisory lock.

    async with db.read("digest page") as unit:           # one snapshot, read-only
        rows = await (await unit.execute("SELECT ...")).fetchall()
    async with db.write("label", taxonomy="shared", papers=[paper_id]) as unit:
        await unit.execute("INSERT ...")                 # commits at exit, rolls back on error

A unit is one short transaction on one connection, and speaks the connection's language
(`execute`, `cursor`, and a cursor's `executemany` and `copy`), rows as dicts. A function that
takes `db` is a boundary and opens its own units; one that takes a unit runs inside it and never
commits. Units do not nest: opening one while another is open on the task, or calling another
gate meanwhile (`outside_unit`), raises with `LITWATCH_STRICT_GATES=1` and is logged as an error
without it.

A write unit names its locks when it opens and the gate takes them in one order: the taxonomy,
then papers by id, then the other locks as `REGISTRY` lists them. The session locks held across
network calls (`HELD_ACROSS_CALLS`) are not units: `db.held` keeps each on a connection of its
own, outside the pool.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, LiteralString, Self

import psycopg
from psycopg import sql
from psycopg.abc import Params
from psycopg.conninfo import make_conninfo
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

log = logging.getLogger(__name__)

type Connection = psycopg.AsyncConnection[dict[str, Any]]
type Cursor = psycopg.AsyncCursor[dict[str, Any]]
type Query = LiteralString | bytes | sql.SQL | sql.Composed
type TaxonomyLock = Literal["shared", "exclusive"]
# A lock other than the taxonomy's and the papers', as a write unit names it: its name, with
# what it is taken for when it has a second key (`("rerun", topic)`).
type Named = str | tuple[str, str | int]

# Set to 1, a nesting violation raises; otherwise it is logged and work carries on. Read at
# each check. Tests and a development `serve` set it (docs/agents/dev-host.md).
STRICT_ENV = "LITWATCH_STRICT_GATES"
# A unit open longer than this is logged as a warning: units are short, and a long one holds
# its locks and holds back the outbox, which waits on the cluster's oldest snapshot.
SLOW_UNIT_SECONDS = 5.0


class NestingError(RuntimeError):
    """A unit was opened, or another gate called, while a unit was open on the task."""


class LateLockError(RuntimeError):
    """A write unit was asked for a lock after it opened."""


class PoolTimeoutError(RuntimeError):
    """A unit waited its whole timeout for a connection from the pool and got none."""


class BusyError(RuntimeError):
    """Another session holds the session lock asked for."""


@dataclass(frozen=True)
class Hold:
    """A session lock `DatabaseGate.held` took: its name and the backend that holds it."""

    name: str
    pid: int


class BridgeError(RuntimeError):
    """A bridged connection (`DatabaseGate.over`) was already inside a transaction."""


# -- the lock registry -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Lock:
    """One advisory lock. Its key is `key` alone, or `key` with a second one made from what the
    lock is taken for (`arg`): a text, hashed by `hashtext`, or an integer as it is.

    `scope` says how long it is held: `unit` to the end of the write unit that named it,
    `session` until its holder lets go (`DatabaseGate.held`), `startup` while a process
    starts."""

    name: str
    key: int
    scope: Literal["unit", "session", "startup"]
    arg: Literal["text", "int"] | None = None


# Every advisory lock litwatch takes, in the order a write unit takes those it names. The keys
# are the ones the code used before the gate, which still takes them itself until it has moved
# onto the gate: both must exclude each other.
REGISTRY: tuple[Lock, ...] = (
    # Held shared by a unit that locks papers and exclusively by a taxonomy edit, so a settle
    # never reads a tree an edit in flight is changing.
    Lock("taxonomy", 7213913, "unit"),
    # Settling one paper: every write that can move it holds its lock.
    Lock("paper", 7213912, "unit", "text"),
    # A paper's published record, while it is compared and stored.
    Lock("publication", 7213914, "unit", "text"),
    # Making a topic's re-runs, so two asking at once make one.
    Lock("rerun", 7213916, "unit", "text"),
    # Making replays, likewise.
    Lock("replay", 7213917, "unit"),
    # One retally of a run's totals at a time, by run id.
    Lock("retally", 7213912, "unit", "int"),
    # One job that changes pipeline state at a time.
    Lock("run", 7213911, "session"),
    # One scheduler deciding and running a job kind.
    Lock("schedule", 7213913, "session", "text"),
    # One delivery to an integration at a time.
    Lock("dispatch", 7213914, "session", "text"),
    # One worker on the Deep passes.
    Lock("deep-worker", 7213915, "session"),
    # Migrations, applied by one process at a time as it starts.
    Lock("migration", 7213910, "startup"),
)
LOCKS = {lock.name: lock for lock in REGISTRY}
_ORDER = {lock.name: position for position, lock in enumerate(REGISTRY)}

# The session locks held across network calls, against the rule that only a unit holds a lock.
# The list only shrinks: A2 turns their loops into Workers and A3 removes the run lock.
HELD_ACROSS_CALLS = ("run", "schedule", "dispatch", "deep-worker")

_ARGS: dict[str | None, LiteralString] = {None: "%s", "text": "%s, hashtext(%s)", "int": "%s, %s"}


def _statement(function: LiteralString, lock: Lock) -> sql.Composed:
    """The call of an advisory-lock function on `lock`; `_params` gives its parameters."""
    return sql.SQL("SELECT {}({}) AS ok").format(sql.SQL(function), sql.SQL(_ARGS[lock.arg]))


def _params(lock: Lock, arg: str | int | None) -> tuple[Any, ...]:
    """The lock's key, and what it is taken for, checked against the kind the lock takes."""
    kind = {"text": str, "int": int}.get(lock.arg or "")
    if kind is None:
        if arg is not None:
            raise ValueError(f"the {lock.name} lock takes no argument, given {arg!r}")
        return (lock.key,)
    if not isinstance(arg, kind) or isinstance(arg, bool):
        raise ValueError(f"the {lock.name} lock is taken for {lock.arg}, given {arg!r}")
    return (lock.key, arg)


def _named(locks: Iterable[Named]) -> list[tuple[Lock, str | int | None]]:
    """A write unit's other locks, each once, in the registry's order and then by argument."""
    found: set[tuple[str, str | int | None]] = set()
    for named in locks:
        name, arg = (named, None) if isinstance(named, str) else named
        lock = LOCKS.get(name)
        if lock is None or lock.scope != "unit":
            raise ValueError(f"{name!r} is not a lock a unit takes")
        if name in ("taxonomy", "paper"):
            raise ValueError(f"the {name} lock is named by `taxonomy` and `papers`")
        _params(lock, arg)
        found.add((name, arg))
    # Each is there once, and one lock's arguments are of one type, so they compare.
    ordered = sorted(found, key=lambda item: (_ORDER[item[0]], item[1]))
    return [(LOCKS[name], arg) for name, arg in ordered]


# -- the nesting check -----------------------------------------------------------------------------

# The unit the task last opened. A task started inside a unit inherits it, and is inside that
# unit while it stays open: its units and model calls run under the parent's locks.
_unit: ContextVar[ReadUnit | None] = ContextVar("litwatch_unit", default=None)


def outside_unit(where: str) -> None:
    """Check that no unit is open on this task. Every gate calls it before its work, naming
    what it is about to do (`where`): slow work happens outside units. A violation raises
    `NestingError` with `LITWATCH_STRICT_GATES=1`, and is logged as an error without it."""
    unit = _unit.get()
    if unit is None or not unit.open:
        return
    message = f"{where} while unit {unit.name!r} is open"
    if os.environ.get(STRICT_ENV) == "1":
        raise NestingError(message)
    log.error("gate nesting: %s", message)


@asynccontextmanager
async def _entered(unit: ReadUnit) -> AsyncIterator[None]:
    """`unit` is the task's open unit for the block. It is closed at the end, and a slow one
    logged."""
    outer = _unit.get()
    _unit.set(unit)
    started = time.monotonic()
    try:
        yield
    finally:
        unit._conn = None
        _unit.set(outer)
        lasted = time.monotonic() - started
        if lasted > SLOW_UNIT_SECONDS:
            log.warning("slow unit: %r was open for %.1fs", unit.name, lasted)


# -- units -----------------------------------------------------------------------------------------


class ReadUnit:
    """One read-only transaction on one snapshot. It holds no advisory lock. Used after it
    closed, it raises: its connection is someone else's by then."""

    def __init__(self, name: str, conn: Connection) -> None:
        self.name = name
        self._conn: Connection | None = conn

    @property
    def open(self) -> bool:
        return self._conn is not None

    def _connection(self) -> Connection:
        if self._conn is None:
            raise RuntimeError(f"unit {self.name!r} is closed")
        return self._conn

    async def execute(
        self,
        query: Query,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool = False,
    ) -> Cursor:
        """As a connection's `execute`: a new cursor with the statement run on it."""
        return await self._connection().execute(query, params, prepare=prepare, binary=binary)

    def cursor(self, *, binary: bool = False) -> Cursor:
        """As a connection's `cursor`."""
        return self._connection().cursor(binary=binary)

    async def executemany(
        self, query: Query, params_seq: Iterable[Params], *, returning: bool = False
    ) -> Cursor:
        """A cursor's `executemany` on a new cursor, which is returned."""
        cur = self.cursor()
        await cur.executemany(query, params_seq, returning=returning)
        return cur

    @asynccontextmanager
    async def copy(
        self, statement: Query, params: Params | None = None
    ) -> AsyncIterator[psycopg.AsyncCopy]:
        """A cursor's `copy` on a cursor of its own: the COPY to read from or write to."""
        async with self.cursor() as cur, cur.copy(statement, params) as copy:
            yield copy


class WriteUnit(ReadUnit):
    """One read-write transaction, holding the locks it named when it opened: `taxonomy` (how
    it holds the taxonomy lock, if at all), `papers` (the ids whose locks it holds) and `locks`
    (the others)."""

    def __init__(self, name: str, conn: Connection, taxonomy: TaxonomyLock | None) -> None:
        super().__init__(name, conn)
        self.taxonomy = taxonomy
        self.locks: tuple[Named, ...] = ()
        self._papers: set[str] = set()

    @property
    def papers(self) -> frozenset[str]:
        return frozenset(self._papers)

    async def lock_papers(self, paper_ids: Iterable[str]) -> None:
        """Lock more papers, found after the unit opened. Only a unit holding the taxonomy lock
        exclusively may: every other unit that locks papers takes the taxonomy lock first, so
        none holds a paper this one could wait on. Any other unit raises `LateLockError`."""
        if self.taxonomy != "exclusive":
            raise LateLockError(
                f"unit {self.name!r} asked for paper locks after it opened; "
                "a unit names its papers when it opens, unless it holds the taxonomy exclusively"
            )
        await self._lock_papers(paper_ids)

    async def _lock_papers(self, paper_ids: Iterable[str]) -> None:
        new = sorted(set(paper_ids) - self._papers)
        if new:
            lock = LOCKS["paper"]
            statement = _statement("pg_advisory_xact_lock", lock)
            await self.executemany(statement, [_params(lock, paper_id) for paper_id in new])
            self._papers.update(new)

    async def _take(
        self, papers: Iterable[str], locks: Sequence[tuple[Lock, str | int | None]]
    ) -> None:
        """Take the unit's locks, in the gate's order."""
        if self.taxonomy is not None:
            shared = self.taxonomy == "shared"
            function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
            lock = LOCKS["taxonomy"]
            await self.execute(_statement(function, lock), _params(lock, None))
        await self._lock_papers(papers)
        for lock, arg in locks:
            await self.execute(_statement("pg_advisory_xact_lock", lock), _params(lock, arg))
        self.locks = tuple(lock.name if arg is None else (lock.name, arg) for lock, arg in locks)


# -- the gate --------------------------------------------------------------------------------------


def _idle(conn: Connection) -> None:
    """Refuse a bridged connection that is inside a transaction: a unit on it would be a
    savepoint of that transaction, and its commit no commit."""
    status = conn.info.transaction_status
    if status != TransactionStatus.IDLE:
        raise BridgeError(
            f"the bridged connection is not idle ({status.name}); a unit is its own transaction"
        )


class DatabaseGate:
    """One pool of connections to one database, and the units and locks on it. One per process;
    open it before use and close it at the end (`async with DatabaseGate(dsn) as db`).

    `schema` puts every connection's search path on that schema (tests' throwaway ones). A unit
    waits `timeout` seconds for a connection, then fails with `PoolTimeoutError`.
    """

    def __init__(
        self,
        dsn: str,
        schema: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 10,
        timeout: float = 30.0,
        _bridged: Connection | None = None,
    ) -> None:
        self._dsn, self._schema, self.timeout = dsn, schema, timeout
        self._bridged = _bridged
        self._pool: AsyncConnectionPool[Connection] | None = None
        if _bridged is None:
            self._pool = AsyncConnectionPool(
                dsn,
                connection_class=psycopg.AsyncConnection[dict[str, Any]],
                kwargs={"row_factory": dict_row, "autocommit": True},
                min_size=min_size,
                max_size=max_size,
                open=False,
                configure=self._configure if schema else None,
                # A connection the server dropped (a restart) is replaced when it is asked for.
                check=AsyncConnectionPool.check_connection,
                name="litwatch",
            )

    @classmethod
    def over(cls, conn: Connection) -> DatabaseGate:
        """A gate whose units all run on `conn`, with no pool: the bridge by which code still
        holding a connection calls code that takes `db`. One unit at a time, as the connection
        serves one coroutine at a time. It refuses a connection inside a transaction, here and
        whenever a unit opens."""
        _idle(conn)
        return cls(make_conninfo(conn.info.dsn, password=conn.info.password), _bridged=conn)

    async def _configure(self, conn: Connection) -> None:
        assert self._schema is not None
        await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self._schema)))

    async def open(self) -> None:
        """Open the pool, waiting until its first connections are made."""
        if self._pool is not None:
            await self._pool.open(wait=True, timeout=self.timeout)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @asynccontextmanager
    async def _connection(self, name: str) -> AsyncIterator[Connection]:
        """A connection for the unit `name`: the pool's, or the bridged one."""
        if self._pool is None:
            assert self._bridged is not None
            _idle(self._bridged)
            yield self._bridged
            return
        try:
            conn = await self._pool.getconn(self.timeout)
        except PoolTimeout as exc:
            # Why is not known here: every connection in use, or none to be had from the server.
            raise PoolTimeoutError(
                f"unit {name!r} waited {self.timeout:g}s for a connection "
                f"from the pool of {self._pool.max_size} and got none"
            ) from exc
        try:
            yield conn
        finally:
            await self._pool.putconn(conn)

    @asynccontextmanager
    async def read(self, name: str) -> AsyncIterator[ReadUnit]:
        """A read unit: a repeatable-read, read-only transaction, so every read in it sees one
        snapshot and Postgres refuses a write. It takes no advisory lock."""
        outside_unit(f"opening unit {name!r}")
        async with self._connection(name) as conn:
            unit = ReadUnit(name, conn)
            async with _entered(unit), conn.transaction():
                await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                yield unit

    @asynccontextmanager
    async def write(
        self,
        name: str,
        *,
        taxonomy: TaxonomyLock | None = None,
        papers: Iterable[str] = (),
        locks: Iterable[Named] = (),
    ) -> AsyncIterator[WriteUnit]:
        """A write unit: a transaction that takes the locks named here as it opens, in the
        gate's order whatever order they are named in, commits at the end of the block and
        rolls back if the block raises.

        `papers` are locked under the taxonomy lock, so naming any needs `taxonomy` too:
        `"shared"` for a write that settles them, `"exclusive"` for a taxonomy edit, which may
        lock more as it finds them (`WriteUnit.lock_papers`). `locks` are the others, by name,
        or name and argument (`("rerun", topic)`)."""
        if taxonomy not in (None, "shared", "exclusive"):
            raise ValueError(f"the taxonomy lock is shared or exclusive, not {taxonomy!r}")
        if isinstance(papers, str):
            raise TypeError(f"papers is a collection of ids, not the id {papers!r}")
        paper_ids = set(papers)
        if paper_ids and taxonomy is None:
            raise ValueError(f"unit {name!r} names papers without the taxonomy lock")
        others = _named(locks)
        outside_unit(f"opening unit {name!r}")
        async with self._connection(name) as conn:
            unit = WriteUnit(name, conn, taxonomy)
            async with _entered(unit), conn.transaction():
                await unit._take(paper_ids, others)
                yield unit

    @asynccontextmanager
    async def held(self, name: str, key_arg: str | int | None = None) -> AsyncIterator[Hold]:
        """Hold the session lock `name` (one of `HELD_ACROSS_CALLS`) for the block, on a
        connection of its own outside the pool; `key_arg` is what a keyed one is taken for (the
        job kind, the integration). Raises `BusyError` when another session holds it, another
        `held` of this gate included: the lock is not taken twice. Yields the `Hold`, which
        `held_elsewhere` takes to leave the holder's own hold out."""
        if name not in HELD_ACROSS_CALLS:
            raise ValueError(f"{name!r} is not a session lock held across calls")
        lock = LOCKS[name]
        params = _params(lock, key_arg)
        label = name if key_arg is None else f"{name} ({key_arg})"
        conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        try:
            taken = await conn.execute(_statement("pg_try_advisory_lock", lock), params)
            if not (await taken.fetchone())[0]:
                raise BusyError(f"the {label} lock is held by another session")
            try:
                yield Hold(name, conn.info.backend_pid)
            finally:
                # A connection that ended let the lock go while the block still ran under it.
                try:
                    freed = await conn.execute(_statement("pg_advisory_unlock", lock), params)
                    lost = not (await freed.fetchone())[0]
                except psycopg.Error:
                    lost = True
                if lost:
                    log.error("the %s lock was lost while held: its connection ended", label)
        finally:
            await conn.close()

    async def held_elsewhere(self, name: str, *, mine: Hold | None = None) -> bool:
        """Whether a session holds the session lock `name`, other than the caller's own `mine`:
        for the run lock, whether a job is running. Another task's hold in this process counts.
        For the locks without an argument."""
        lock = LOCKS[name]
        if name not in HELD_ACROSS_CALLS or lock.arg is not None:
            raise ValueError(f"{name!r} is not a session lock without an argument")
        # Our keys fit in the low half of the one-key form: classid 0, the key in objid.
        query = (
            "SELECT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
            "WHERE d.datname = current_database() AND l.locktype = 'advisory' AND l.granted "
            "AND l.classid = 0 AND l.objid = %s AND l.objsubid = 1 "
            "AND l.pid IS DISTINCT FROM %s) AS held"
        )
        if mine is not None and mine.name != name:
            raise ValueError(f"the hold given is {mine.name!r}'s, not {name!r}'s")
        params = (lock.key, mine.pid if mine else None)
        if self._bridged is not None:  # a plain read: inside a transaction or not
            return (await (await self._bridged.execute(query, params)).fetchone())["held"]
        async with self._connection(f"held_elsewhere {name}") as conn:
            return (await (await conn.execute(query, params)).fetchone())["held"]
