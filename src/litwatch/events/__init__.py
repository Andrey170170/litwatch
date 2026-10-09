"""The outbox. A pipeline step writes its event in the same transaction as its data."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

PAPER_ADDED = "paper.added"
PAPER_REVISED = "paper.revised"
VERDICT_RECORDED = "verdict.recorded"


async def emit(
    conn: psycopg.AsyncConnection, type: str, paper_id: str | None = None, **payload: Any
) -> None:
    """Record an event. Call it inside the transaction that writes what it describes."""
    await conn.execute(
        "INSERT INTO outbox (type, paper_id, payload) VALUES (%s, %s, %s)",
        (type, paper_id, Jsonb(payload)),
    )
