"""Intake's checkpoints: per category (`*` for the catalog pass), the first day it was asked to
read and the day its next harvest goes on from."""

from __future__ import annotations

from datetime import date

import psycopg


async def start(conn: psycopg.AsyncConnection, category: str, since: date) -> date:
    """The day a harvest of `category` starts from: its checkpoint, else the first day asked,
    `since` for a category never read."""
    await conn.execute(
        "INSERT INTO intake_checkpoints(category,discovery_from) VALUES (%s,%s) "
        "ON CONFLICT(category) DO NOTHING",
        (category, since),
    )
    row = await (
        await conn.execute(
            "SELECT coalesce(checkpoint,discovery_from) AS start FROM intake_checkpoints "
            "WHERE category=%s",
            (category,),
        )
    ).fetchone()
    assert row is not None
    return row["start"]


async def lower(conn: psycopg.AsyncConnection, category: str, day: date) -> None:
    """Have the next harvest start from `day`, unless one was already asked from `day` or
    earlier: then it goes on from its checkpoint, so asking again resumes."""
    await conn.execute(
        "INSERT INTO intake_checkpoints(category,discovery_from) VALUES (%s,%s) "
        "ON CONFLICT(category) DO UPDATE SET "
        "checkpoint=CASE WHEN %s<intake_checkpoints.discovery_from THEN NULL "
        "ELSE intake_checkpoints.checkpoint END, "
        "discovery_from=least(intake_checkpoints.discovery_from,%s)",
        (category, day, day, day),
    )


async def advance(conn: psycopg.AsyncConnection, category: str, day: date, run_id: int) -> None:
    """A harvest of `category` is complete through `day`; the checkpoint never moves back."""
    await conn.execute(
        "UPDATE intake_checkpoints SET checkpoint=greatest(checkpoint,%s), "
        "checked_at=now(),run_id=%s WHERE category=%s",
        (day, run_id, category),
    )
