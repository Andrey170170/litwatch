"""The tests' own sessions: a connection to a test's schema, a second one beside it, a gate
whose units run on a given connection (`Bridge`), and a store function called in a unit of its
own on one (`in_read`, `in_write`). Only tests hold a connection; the code reaches the database
through the Database gate's pool (`gates.database`)."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Concatenate
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from litwatch import store
from litwatch.gates.database import (
    Connection,
    DatabaseGate,
    Identity,
    ReadUnit,
    WriteUnit,
    identity_of,
    outside_unit,
)


class SessionInStoreError(RuntimeError):
    """A store function was handed a test's session where it takes a unit."""


def _not_from_store() -> None:
    """Refuse SQL a store function runs on a test's session: it takes a unit (`in_read`,
    `in_write`), whatever name the session went by. Migrations take a connection."""
    caller = sys._getframe(2).f_globals.get("__name__", "")
    if caller.startswith("litwatch.store.") and caller != "litwatch.store.db":
        raise SessionInStoreError(f"{caller} ran SQL on a test's session; hand it a unit")


class Session(psycopg.AsyncConnection[dict[str, Any]]):
    """A test's connection. One made by `schema` names its database and schema, for the gates
    a test opens on them. A store function handed one raises (`SessionInStoreError`). Typed so
    that ty checks what a test hands to litwatch, and not the SQL a test checks the database
    with itself, whose rows it reads as it knows them."""

    test_dsn: str
    test_schema: str

    if TYPE_CHECKING:

        async def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any: ...

    else:

        def execute(self, *args, **kwargs):
            _not_from_store()
            return super().execute(*args, **kwargs)

        def cursor(self, *args, **kwargs):
            _not_from_store()
            return super().cursor(*args, **kwargs)


async def connect(dsn: str, schema: str | None = None) -> Session:
    """An autocommit connection returning rows as dicts, on `schema` when given. One connection
    serves one coroutine at a time; concurrent work takes its own."""
    conn = await Session.connect(dsn, row_factory=dict_row, autocommit=True)
    if schema:
        await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    return conn


@asynccontextmanager
async def schema(dsn: str) -> AsyncIterator[Session]:
    """A connection to a new, empty, migrated schema of `dsn`'s database, which is dropped at
    the end."""
    name = "test_" + uuid4().hex
    async with await connect(dsn) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            async with await connect(dsn, name) as conn:
                await store.migrate(conn)
                conn.test_dsn, conn.test_schema = dsn, name
                yield conn
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _dsn(conn: Connection) -> str:
    return make_conninfo(conn.info.dsn, password=conn.info.password)


async def sibling(conn: Connection) -> Session:
    """A second session on `conn`'s database and schema, as another process's: for work that
    runs alongside, or a lock held elsewhere."""
    other = await connect(_dsn(conn))
    path = (await (await conn.execute("SHOW search_path")).fetchone())["search_path"]
    await other.execute(sql.SQL("SET search_path TO {}").format(sql.SQL(path)))
    return other


def staged(conn: Connection, name: str = "staged") -> WriteUnit:
    """`conn` as a write unit, for a store function called on a session a test stages as
    another process's (`sibling`): the test holds that session's transaction itself
    (`conn.transaction()`), or none, each statement then its own. It takes no lock, and the
    nesting check does not see it, so the test can go on while that transaction is open."""
    return WriteUnit(name, conn, None)


class BridgeError(RuntimeError):
    """A bridged connection was already inside a transaction."""


def _idle(conn: Connection) -> None:
    """Refuse a connection inside a transaction: a unit on it would be a savepoint of that
    transaction, and its commit no commit."""
    status = conn.info.transaction_status
    if status != TransactionStatus.IDLE:
        raise BridgeError(
            f"the bridged connection is not idle ({status.name}); a unit is its own transaction"
        )


class Bridge(DatabaseGate):
    """A Database gate whose units all run on one given connection, with no pool: a Service
    under test then works on the test's own session, and its run records and job claims name
    that session (`identity`). One unit at a time, as the connection serves one coroutine at a
    time. The session locks (`held`) take connections of their own, as on any gate. It refuses
    a connection inside a transaction, here and whenever a unit opens, so nesting stays
    strict. Nothing to open or close: the connection is the test's."""

    def __init__(self, conn: Connection) -> None:
        _idle(conn)
        super().__init__(_dsn(conn))
        self._conn = conn

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def identity(self) -> Identity:
        """The connection's own session."""
        outside_unit("asking for the presence session's identity")
        if self._identity is None:
            self._identity = await identity_of(self._conn)
        return self._identity

    @asynccontextmanager
    async def _connection(self, name: str) -> AsyncIterator[Connection]:
        del name  # every unit's connection is the one given
        _idle(self._conn)
        yield self._conn


# A test's seeds and checks through `store`: one store function, called in a unit of its own on
# the test's connection, committed before the next statement. A seed of several calls that
# must stand or fall together opens `Bridge(conn).write(...)` itself.


async def in_read[**P, T](
    conn: Connection,
    read: Callable[Concatenate[ReadUnit, P], Awaitable[T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """`read(unit, *args, **kwargs)` in a read unit on `conn`."""
    async with Bridge(conn).read(_name(read)) as unit:
        return await read(unit, *args, **kwargs)


async def in_write[**P, T](
    conn: Connection,
    write: Callable[Concatenate[WriteUnit, P], Awaitable[T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """`write(unit, *args, **kwargs)` in a write unit on `conn`, which takes no lock."""
    async with Bridge(conn).write(_name(write)) as unit:
        return await write(unit, *args, **kwargs)


def _name(function: object) -> str:
    return getattr(function, "__qualname__", "test")
