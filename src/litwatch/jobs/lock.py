"""One writer at a time. Jobs that change pipeline state hold this database lock, so two
instances, or the scheduler and a terminal, never overlap."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg

RUN_LOCK = 7213911


class BusyError(RuntimeError):
    """Another litwatch job holds the run lock."""


@asynccontextmanager
async def run_lock(conn: psycopg.AsyncConnection) -> AsyncIterator[None]:
    row = await (
        await conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (RUN_LOCK,))
    ).fetchone()
    if not row["locked"]:
        raise BusyError("another litwatch job is already running")
    try:
        yield
    finally:
        await conn.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK,))


async def held_elsewhere(conn: psycopg.AsyncConnection) -> bool:
    """Whether another session holds the run lock: a job is running."""
    row = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
            "WHERE d.datname = current_database() AND l.locktype = 'advisory' AND l.granted "
            "AND l.classid = 0 AND l.objid = %s AND l.objsubid = 1 "
            "AND l.pid <> pg_backend_pid()) AS held",
            (RUN_LOCK,),
        )
    ).fetchone()
    return row["held"]
