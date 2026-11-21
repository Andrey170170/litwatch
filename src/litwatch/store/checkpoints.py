"""Intake's checkpoints: per category (`*` for the catalog pass), the first day it was asked to
read and the day its next harvest goes on from; and the Atom feeds it archived, per category."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from litwatch.gates.database import ReadUnit, WriteUnit


async def start(unit: WriteUnit, category: str, since: date) -> date:
    """The day a harvest of `category` starts from: its checkpoint, else the first day asked,
    `since` for a category never read."""
    await unit.execute(
        "INSERT INTO intake_checkpoints(category,discovery_from) VALUES (%s,%s) "
        "ON CONFLICT(category) DO NOTHING",
        (category, since),
    )
    row = await (
        await unit.execute(
            "SELECT coalesce(checkpoint,discovery_from) AS start FROM intake_checkpoints "
            "WHERE category=%s",
            (category,),
        )
    ).fetchone()
    assert row is not None
    return row["start"]


async def opened(unit: WriteUnit, category: str, initial: date, since: date) -> dict[str, Any]:
    """`category`'s checkpoint row, made from `initial` the first time; asked again, its first
    day moves back to `since` if that is earlier, never forward."""
    await unit.execute(
        "INSERT INTO intake_checkpoints(category,discovery_from) VALUES (%s,%s) "
        "ON CONFLICT(category) DO UPDATE SET "
        "discovery_from=least(intake_checkpoints.discovery_from,%s)",
        (category, initial, since),
    )
    row = await (
        await unit.execute("SELECT * FROM intake_checkpoints WHERE category=%s", (category,))
    ).fetchone()
    assert row is not None
    return row


async def lower(unit: WriteUnit, category: str, day: date) -> None:
    """Have the next harvest start from `day`, unless one was already asked from `day` or
    earlier: then it goes on from its checkpoint, so asking again resumes."""
    await unit.execute(
        "INSERT INTO intake_checkpoints(category,discovery_from) VALUES (%s,%s) "
        "ON CONFLICT(category) DO UPDATE SET "
        "checkpoint=CASE WHEN %s<intake_checkpoints.discovery_from THEN NULL "
        "ELSE intake_checkpoints.checkpoint END, "
        "discovery_from=least(intake_checkpoints.discovery_from,%s)",
        (category, day, day, day),
    )


async def advance(unit: WriteUnit, category: str, day: date, run_id: int) -> None:
    """A harvest of `category` is complete through `day`; the checkpoint never moves back."""
    await unit.execute(
        "UPDATE intake_checkpoints SET checkpoint=greatest(checkpoint,%s), "
        "checked_at=now(),run_id=%s WHERE category=%s",
        (day, run_id, category),
    )


async def archive_feed(
    unit: WriteUnit, category: str, sha256: str, body: bytes, generated_at: datetime
) -> None:
    """Keep a category's Atom feed as it was read; the same bytes are kept once."""
    await unit.execute(
        "INSERT INTO intake_snapshots(category,sha256,body,generated_at) "
        "VALUES(%s,%s,%s,%s) ON CONFLICT(category,sha256) DO NOTHING",
        (category, sha256, body, generated_at),
    )


async def feeds(unit: ReadUnit, category: str, since: date) -> list[bytes]:
    """The category's archived Atom feeds generated on `since` or after, oldest first."""
    rows = await (
        await unit.execute(
            "SELECT body FROM intake_snapshots WHERE category=%s AND generated_at >= %s "
            "ORDER BY generated_at",
            (category, datetime.combine(since, datetime.min.time(), UTC)),
        )
    ).fetchall()
    return [bytes(row["body"]) for row in rows]
