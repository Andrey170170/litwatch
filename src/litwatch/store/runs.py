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
    row = await (
        await conn.execute(
            "INSERT INTO runs (kind, window_from, window_to) VALUES (%s, %s, %s) RETURNING id",
            (kind, window_from, window_to),
        )
    ).fetchone()
    return row["id"]


async def finish(conn: psycopg.AsyncConnection, run_id: int, **fields: Any) -> None:
    """Close the run, setting any of fetched, inserted, updated, cost, error, counts, intake."""
    if unknown := set(fields) - _FIELDS:
        raise ValueError(f"unknown run fields: {sorted(unknown)}")
    sets = [sql.SQL("finished_at = now()")]
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
