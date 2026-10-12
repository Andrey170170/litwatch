"""The outbox as integrations read it: events after a position, and one cursor per integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg

from litwatch.core import Event


@dataclass(frozen=True)
class Cursor:
    name: str
    position: int
    failing: int | None = None
    failures: int = 0
    retry_at: datetime | None = None
    last_error: str | None = None
    failed_at: datetime | None = None
    skipped: int = 0
    lag: int = 0  # events after the position, wanted or not


_FIELDS = "name, position, failing, failures, retry_at, last_error, failed_at, skipped"


async def end(conn: psycopg.AsyncConnection) -> int:
    row = await (await conn.execute("SELECT coalesce(max(id), 0) AS id FROM outbox")).fetchone()
    return row["id"]


async def after(conn: psycopg.AsyncConnection, position: int, limit: int) -> list[Event]:
    rows = await (
        await conn.execute(
            "SELECT id, type, paper_id, payload, created_at FROM outbox "
            "WHERE id > %s ORDER BY id LIMIT %s",
            (position, limit),
        )
    ).fetchall()
    return [Event(**r) for r in rows]


async def cursor(conn: psycopg.AsyncConnection, name: str) -> Cursor:
    """The integration's cursor, made at the outbox's current end the first time it is asked
    for: an integration enabled for the first time is not woken by what came before it."""
    await conn.execute(
        "INSERT INTO integration_cursors (name, position) "
        "SELECT %s, coalesce(max(id), 0) FROM outbox ON CONFLICT (name) DO NOTHING",
        (name,),
    )
    row = await (
        await conn.execute(f"SELECT {_FIELDS} FROM integration_cursors WHERE name = %s", (name,))
    ).fetchone()
    return Cursor(**row)


async def cursors(conn: psycopg.AsyncConnection, names: list[str]) -> dict[str, Cursor]:
    """The named cursors that exist, each with its lag."""
    rows = await (
        await conn.execute(
            f"SELECT {_FIELDS}, (SELECT count(*) FROM outbox o WHERE o.id > c.position) AS lag "
            "FROM integration_cursors c WHERE name = ANY(%s)",
            (names,),
        )
    ).fetchall()
    return {r["name"]: Cursor(**r) for r in rows}


async def advance(conn: psycopg.AsyncConnection, name: str, position: int) -> None:
    """Everything up to `position` is done; a failure before it is over."""
    await conn.execute(
        "UPDATE integration_cursors SET position = %s, failing = NULL, failures = 0, "
        "retry_at = NULL, updated_at = now() WHERE name = %s AND position < %s",
        (position, name, position),
    )


async def failed(
    conn: psycopg.AsyncConnection,
    name: str,
    event_id: int,
    failures: int,
    error: str,
    retry_at: datetime,
) -> None:
    await conn.execute(
        "UPDATE integration_cursors SET failing = %s, failures = %s, last_error = %s, "
        "failed_at = now(), retry_at = %s, updated_at = now() WHERE name = %s",
        (event_id, failures, error, retry_at, name),
    )


async def retry(conn: psycopg.AsyncConnection, name: str) -> bool:
    """Try the failing event again on the next pass, without waiting out the backoff."""
    cur = await conn.execute(
        "UPDATE integration_cursors SET retry_at = NULL, updated_at = now() "
        "WHERE name = %s AND failing IS NOT NULL",
        (name,),
    )
    return cur.rowcount > 0


async def skip(conn: psycopg.AsyncConnection, name: str) -> int | None:
    """Give up on the failing event and go on after it. Returns its id."""
    row = await (
        await conn.execute(
            "UPDATE integration_cursors SET position = failing, failing = NULL, failures = 0, "
            "retry_at = NULL, skipped = skipped + 1, updated_at = now() "
            "WHERE name = %s AND failing IS NOT NULL RETURNING position",
            (name,),
        )
    ).fetchone()
    return row["position"] if row else None


async def event(conn: psycopg.AsyncConnection, event_id: int) -> Event | None:
    row = await (
        await conn.execute(
            "SELECT id, type, paper_id, payload, created_at FROM outbox WHERE id = %s",
            (event_id,),
        )
    ).fetchone()
    return Event(**row) if row else None
