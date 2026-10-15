"""Migrations: a fresh database, and the refusal of a 0.x one."""

import pytest

from litwatch import store


async def tables(conn) -> set[str]:
    rows = await (
        await conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    ).fetchall()
    return {r["tablename"] for r in rows}


async def test_migrations_apply_once(conn):
    assert await store.migrate(conn) == []
    assert {"papers", "runs", "inference_calls", "outbox"} <= await tables(conn)


async def test_a_0x_database_is_refused(conn):
    # The schema as 0.x left it: no migration record, its own selections.
    for table in await tables(conn):
        await conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    await conn.execute("CREATE TABLE selections (slug text PRIMARY KEY, name text NOT NULL)")
    with pytest.raises(store.LegacyDatabaseError):
        await store.migrate(conn)
    assert await tables(conn) == {"selections"}
