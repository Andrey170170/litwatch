"""Connections and numbered migrations."""

from __future__ import annotations

import logging
import re
from importlib import resources
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

MIGRATION_LOCK = 7213910
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class LegacyDatabaseError(RuntimeError):
    """The database belongs to litwatch 0.x and has not been adopted."""


async def connect(dsn: str, schema: str | None = None) -> psycopg.AsyncConnection[dict[str, Any]]:
    """An autocommit connection returning rows as dicts. Group writes with `conn.transaction()`.

    One connection serves one coroutine at a time; concurrent work takes its own.
    """
    conn = await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row, autocommit=True)
    if schema:
        await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    return conn


async def sibling(conn: psycopg.AsyncConnection) -> psycopg.AsyncConnection[dict[str, Any]]:
    """A second connection to the same database and schema, for work that runs alongside."""
    dsn = make_conninfo(conn.info.dsn, password=conn.info.password)
    other = await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row, autocommit=True)
    path = (await (await conn.execute("SHOW search_path")).fetchone())["search_path"]
    await other.execute(sql.SQL("SET search_path TO " + path))
    return other


async def _exists(conn: psycopg.AsyncConnection, table: str) -> bool:
    row = await (await conn.execute("SELECT to_regclass(%s) AS oid", (table,))).fetchone()
    return row["oid"] is not None


def _migrations() -> list[tuple[int, str, str]]:
    root = resources.files("litwatch.store").joinpath("migrations")
    found = []
    for entry in root.iterdir():
        if match := _NAME.match(entry.name):
            found.append((int(match.group(1)), entry.name, entry.read_text()))
    return sorted(found)


async def migrate(conn: psycopg.AsyncConnection) -> list[str]:
    """Apply every migration not yet applied, in order. Returns the names applied.

    A 0.x database is refused until `adopt_0x` has run on it, so pointing v1 at the wrong
    database by accident changes nothing.
    """
    await conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
    try:
        if not await _exists(conn, "schema_migrations"):
            if await _exists(conn, "selections"):
                raise LegacyDatabaseError(
                    "this is a litwatch 0.x database; run `litwatch db adopt` on a copy of it"
                )
            await conn.execute(
                "CREATE TABLE schema_migrations (version integer PRIMARY KEY, "
                "name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
            )
        rows = await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()
        done = {r["version"] for r in rows}
        applied = []
        for version, name, body in _migrations():
            if version in done:
                continue
            async with conn.transaction():
                await conn.execute(body)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                    (version, name),
                )
            log.info("applied migration %s", name)
            applied.append(name)
        return applied
    finally:
        await conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))


async def adopt_0x(conn: psycopg.AsyncConnection) -> list[str]:
    """Turn a copy of a 0.x database into a v1 one, keeping its papers, runs and journal.

    The 0.x `verdicts` and `selections` tables are renamed with a `legacy_` prefix; their
    content is what the topic import and the regression set read. Then migrations apply.
    """
    if await _exists(conn, "schema_migrations"):
        raise RuntimeError("this database already has v1 migrations")
    if not await _exists(conn, "selections"):
        raise RuntimeError("no 0.x tables here; a fresh database needs only `migrate`")
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE verdicts RENAME TO legacy_verdicts;
            ALTER INDEX verdicts_pkey RENAME TO legacy_verdicts_pkey;
            ALTER INDEX verdicts_selection_tier_idx RENAME TO legacy_verdicts_selection_tier_idx;
            ALTER TABLE selections RENAME TO legacy_selections;
            CREATE TABLE schema_migrations (version integer PRIMARY KEY, name text NOT NULL,
                                            applied_at timestamptz NOT NULL DEFAULT now());
            """
        )
    return await migrate(conn)
