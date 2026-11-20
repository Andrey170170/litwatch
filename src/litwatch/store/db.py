"""Numbered migrations."""

from __future__ import annotations

import logging
import re
from importlib import resources
from typing import Any

import psycopg

log = logging.getLogger(__name__)

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class LegacyDatabaseError(RuntimeError):
    """The database belongs to litwatch 0.x, which v1 never uses (decision 48)."""


async def _exists(conn: psycopg.AsyncConnection[Any], table: str) -> bool:
    row = await (await conn.execute("SELECT to_regclass(%s) AS oid", (table,))).fetchone()
    return row["oid"] is not None


def _migrations() -> list[tuple[int, str, str]]:
    root = resources.files("litwatch.store").joinpath("migrations")
    found = []
    for entry in root.iterdir():
        if match := _NAME.match(entry.name):
            found.append((int(match.group(1)), entry.name, entry.read_text()))
    return sorted(found)


async def migrate(conn: psycopg.AsyncConnection[Any]) -> list[str]:
    """Apply every migration not yet applied, in order. Returns the names applied. A process
    runs it on the migration lock's own session (`DatabaseGate.startup("migration")`), so two
    never apply one twice, and it cannot go on after the lock went with its session.

    A 0.x database is refused, so pointing v1 at the wrong database by accident changes
    nothing.
    """
    if not await _exists(conn, "schema_migrations"):
        if await _exists(conn, "selections"):
            raise LegacyDatabaseError(
                "this is a litwatch 0.x database; v1 starts from an empty one"
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
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, name)
            )
        log.info("applied migration %s", name)
        applied.append(name)
    return applied
