"""Published versions as looked up, or as the owner set them."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Publication

# A library paper with no published record is looked up again monthly for two years.
RECHECK = timedelta(days=30)
GIVE_UP = timedelta(days=730)


async def get(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    return await (
        await conn.execute("SELECT * FROM publications WHERE paper_id = %s", (paper_id,))
    ).fetchone()


async def found(conn: psycopg.AsyncConnection, ids: list[str]) -> dict[str, Publication]:
    """The stored published records of these papers, by paper id."""
    rows = await (
        await conn.execute(
            "SELECT paper_id, record FROM publications "
            "WHERE paper_id = ANY(%s) AND record IS NOT NULL",
            (ids,),
        )
    ).fetchall()
    return {r["paper_id"]: Publication.model_validate(r["record"]) for r in rows}


async def put(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    record: Publication | None,
    *,
    by_owner: bool = False,
) -> tuple[bool, Publication | None]:
    """Store a lookup's result, or the owner's choice. Returns whether it was stored (a
    lookup never replaces the owner's choice) and the record stored before. Call it inside
    a transaction: the row is locked while the two are compared."""
    before = await (
        await conn.execute(
            "SELECT record FROM publications WHERE paper_id = %s FOR UPDATE", (paper_id,)
        )
    ).fetchone()
    stored = await conn.execute(
        """
        INSERT INTO publications AS p (paper_id, record, by_owner) VALUES (%s, %s, %s)
        ON CONFLICT (paper_id) DO UPDATE SET record = EXCLUDED.record,
            by_owner = EXCLUDED.by_owner, checked_at = clock_timestamp()
        WHERE NOT p.by_owner OR EXCLUDED.by_owner
        """,
        (paper_id, Jsonb(record.model_dump(mode="json")) if record else None, by_owner),
    )
    previous = before["record"] if before else None
    return stored.rowcount == 1, Publication.model_validate(previous) if previous else None


async def forget(conn: psycopg.AsyncConnection, paper_id: str) -> None:
    """Drop what is stored, the owner's choice included: the paper is looked up afresh."""
    await conn.execute("DELETE FROM publications WHERE paper_id = %s", (paper_id,))


async def due(conn: psycopg.AsyncConnection, limit: int) -> list[str]:
    """Library papers to look up: those never looked up, newest saves first, then those
    still without a published record a month after the last look, within two years of
    the first."""
    rows = await (
        await conn.execute(
            """
            SELECT l.paper_id FROM library l
            LEFT JOIN publications p ON p.paper_id = l.paper_id
            WHERE l.saved AND (p.paper_id IS NULL OR (
                p.record IS NULL AND NOT p.by_owner
                AND p.checked_at < now() - %(recheck)s
                AND p.first_checked_at > now() - %(give_up)s))
            ORDER BY p.checked_at NULLS FIRST, l.saved_at DESC NULLS LAST, l.paper_id
            LIMIT %(limit)s
            """,
            {"recheck": RECHECK, "give_up": GIVE_UP, "limit": limit},
        )
    ).fetchall()
    return [r["paper_id"] for r in rows]
