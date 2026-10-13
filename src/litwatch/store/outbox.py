"""The outbox as integrations read it: events after a position, and one cursor per integration.

An event's place is its `Position`, (xid, id): the writing transaction's id, then the event's
own. Only events whose transaction is older than every transaction still running are read
(`_SETTLED`), so an event that commits late can never land behind a position already passed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

import psycopg

from litwatch.core import Event

_EVENT = "xid::text::bigint AS xid, id, type, paper_id, payload, created_at"
# Every transaction below the snapshot's xmin has finished: its events are all there is.
_SETTLED = "xid < pg_snapshot_xmin(pg_current_snapshot())"
_TOKEN = re.compile(r"^(\d{1,19})\.(\d{1,19})$")


class Position(NamedTuple):
    xid: int
    id: int

    @property
    def token(self) -> str:
        """The position as the changes feed hands it out: opaque to clients."""
        return f"{self.xid}.{self.id}"

    @classmethod
    def parse(cls, token: str) -> Position:
        if not (match := _TOKEN.match(token)):
            raise ValueError(f"not a position: {token!r}")
        return cls(int(match[1]), int(match[2]))

    @classmethod
    def of(cls, event: Event) -> Position:
        return cls(event.xid or 0, event.id or 0)


START = Position(0, 0)


@dataclass(frozen=True)
class Cursor:
    name: str
    position: Position
    failing: int | None = None
    failures: int = 0
    retry_at: datetime | None = None
    last_error: str | None = None
    failed_at: datetime | None = None
    skipped: int = 0
    lag: int = 0  # events after the position, wanted or not


_FIELDS = (
    "name, position_xid::text::bigint AS position_xid, position, failing, failures, retry_at, "
    "last_error, failed_at, skipped"
)


def _cursor(row: dict) -> Cursor:
    row = dict(row)
    row["position"] = Position(row.pop("position_xid"), row["position"])
    return Cursor(**row)


async def after(conn: psycopg.AsyncConnection, position: Position, limit: int) -> list[Event]:
    """The settled events after `position`, in order."""
    rows = await (
        await conn.execute(
            f"SELECT {_EVENT} FROM outbox WHERE (xid, id) > (%s::text::xid8, %s) "
            f"AND {_SETTLED} ORDER BY xid, id LIMIT %s",
            (str(position.xid), position.id, limit),
        )
    ).fetchall()
    return [Event(**r) for r in rows]


async def cursor(conn: psycopg.AsyncConnection, name: str) -> Cursor:
    """The integration's cursor, made at the last settled event the first time it is asked
    for: an integration enabled for the first time is not woken by what came before it."""
    await conn.execute(
        f"""
        INSERT INTO integration_cursors (name, position_xid, position)
        SELECT %s, coalesce(e.xid, '0'::xid8), coalesce(e.id, 0)
        FROM (SELECT 1) one LEFT JOIN LATERAL (
            SELECT xid, id FROM outbox WHERE {_SETTLED} ORDER BY xid DESC, id DESC LIMIT 1
        ) e ON true
        ON CONFLICT (name) DO NOTHING
        """,
        (name,),
    )
    row = await (
        await conn.execute(f"SELECT {_FIELDS} FROM integration_cursors WHERE name = %s", (name,))
    ).fetchone()
    return _cursor(row)


async def cursors(conn: psycopg.AsyncConnection, names: list[str]) -> dict[str, Cursor]:
    """The named cursors that exist, each with its lag."""
    rows = await (
        await conn.execute(
            f"SELECT {_FIELDS}, (SELECT count(*) FROM outbox o "
            "WHERE (o.xid, o.id) > (c.position_xid, c.position)) AS lag "
            "FROM integration_cursors c WHERE name = ANY(%s)",
            (names,),
        )
    ).fetchall()
    return {r["name"]: _cursor(r) for r in rows}


async def advance(conn: psycopg.AsyncConnection, name: str, position: Position) -> None:
    """Everything up to `position` is done; a failure before it is over."""
    await conn.execute(
        "UPDATE integration_cursors SET position_xid = %s::text::xid8, position = %s, "
        "failing = NULL, failures = 0, retry_at = NULL, updated_at = now() "
        "WHERE name = %s AND (position_xid, position) < (%s::text::xid8, %s)",
        (str(position.xid), position.id, name, str(position.xid), position.id),
    )


async def failed(
    conn: psycopg.AsyncConnection,
    name: str,
    event: Event,
    failures: int,
    error: str,
    failed_at: datetime,
    retry_at: datetime,
) -> bool:
    """Record that `event` failed, unless the cursor has meanwhile gone past it (the owner
    skipped it while it was being tried). Returns whether it was recorded."""
    at = Position.of(event)
    cur = await conn.execute(
        "UPDATE integration_cursors SET failing = %s, failures = %s, last_error = %s, "
        "failed_at = %s, retry_at = %s, updated_at = now() "
        "WHERE name = %s AND (position_xid, position) < (%s::text::xid8, %s)",
        (event.id, failures, error, failed_at, retry_at, name, str(at.xid), at.id),
    )
    return cur.rowcount > 0


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
            "UPDATE integration_cursors c SET position_xid = o.xid, position = o.id, "
            "failing = NULL, failures = 0, retry_at = NULL, skipped = skipped + 1, "
            "updated_at = now() FROM outbox o "
            "WHERE c.name = %s AND o.id = c.failing RETURNING o.id",
            (name,),
        )
    ).fetchone()
    return row["id"] if row else None


async def event(conn: psycopg.AsyncConnection, event_id: int) -> Event | None:
    row = await (
        await conn.execute(f"SELECT {_EVENT} FROM outbox WHERE id = %s", (event_id,))
    ).fetchone()
    return Event(**row) if row else None
