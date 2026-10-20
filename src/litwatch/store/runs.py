"""Run records: one row per execution of a job, with its window, counts, cost and error."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

_JSON = {"counts", "intake"}
_FIELDS = {"fetched", "inserted", "updated", "cost", "error", *_JSON}


async def start(
    conn: psycopg.AsyncConnection,
    kind: str,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
) -> int:
    """Open a run on `conn`, the connection its job keeps for as long as it runs: the run records
    that session, and is alive while it lasts (`ALIVE`)."""
    row = await (
        await conn.execute(
            "INSERT INTO runs (kind, window_from, window_to, backend_pid, backend_start) "
            "SELECT %s, %s, %s, pid, backend_start FROM pg_stat_activity "
            "WHERE pid = pg_backend_pid() RETURNING id",
            (kind, window_from, window_to),
        )
    ).fetchone()
    return row["id"]


# Whether run `r`'s process still has its session. A process that dies without closing its run
# (killed, out of memory) loses its session with it, so its run is over: open, not alive.
ALIVE = (
    "EXISTS (SELECT 1 FROM pg_stat_activity a "
    "WHERE a.pid = r.backend_pid AND a.backend_start = r.backend_start)"
)


async def latest(conn: psycopg.AsyncConnection, kind: str) -> dict[str, Any] | None:
    """The newest run of `kind`, with `alive`."""
    return await (
        await conn.execute(
            f"SELECT r.*, {ALIVE} AS alive FROM runs r WHERE kind = %s ORDER BY id DESC LIMIT 1",
            (kind,),
        )
    ).fetchone()


async def latest_finished(conn: psycopg.AsyncConnection, kind: str) -> dict[str, Any] | None:
    """The newest run of `kind` that finished, with an error or not. One whose process died has
    no known end, nor its counts."""
    return await (
        await conn.execute(
            "SELECT * FROM runs WHERE kind = %s AND finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (kind,),
        )
    ).fetchone()


async def last_success(conn: psycopg.AsyncConnection, kind: str) -> dict[str, Any] | None:
    """The newest run of `kind` that finished without an error."""
    return await (
        await conn.execute(
            "SELECT * FROM runs WHERE kind = %s AND finished_at IS NOT NULL AND error IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (kind,),
        )
    ).fetchone()


async def first_since(conn: psycopg.AsyncConnection, kind: str, since: datetime) -> datetime | None:
    """When the first run of `kind` started at or after `since`."""
    row = await (
        await conn.execute(
            "SELECT min(started_at) AS at FROM runs WHERE kind = %s AND started_at >= %s",
            (kind, since),
        )
    ).fetchone()
    return row["at"]


async def update(conn: psycopg.AsyncConnection, run_id: int, **fields: Any) -> None:
    """Set any of fetched, inserted, updated, cost, error, counts, intake on a run that is
    still going."""
    await _set(conn, run_id, [], fields)


async def finish(conn: psycopg.AsyncConnection, run_id: int, **fields: Any) -> None:
    """Close the run, setting any of the fields `update` takes."""
    await _set(conn, run_id, [sql.SQL("finished_at = now()")], fields)


async def _set(
    conn: psycopg.AsyncConnection, run_id: int, sets: list[sql.Composable], fields: dict[str, Any]
) -> None:
    if unknown := set(fields) - _FIELDS:
        raise ValueError(f"unknown run fields: {sorted(unknown)}")
    if not sets and not fields:
        return
    values = []
    for name, value in fields.items():
        sets.append(sql.SQL("{} = %s").format(sql.Identifier(name)))
        values.append(Jsonb(value) if name in _JSON and value is not None else value)
    await conn.execute(
        sql.SQL("UPDATE runs SET {} WHERE id = %s").format(sql.SQL(", ").join(sets)),
        (*values, run_id),
    )


async def get(conn: psycopg.AsyncConnection, run_id: int) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,))).fetchone()


async def recent(conn: psycopg.AsyncConnection, limit: int = 20) -> list[dict[str, Any]]:
    return await (
        await conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT %s", (limit,))
    ).fetchall()
