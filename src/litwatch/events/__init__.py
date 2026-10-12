"""The outbox. A pipeline step writes its event in the same transaction as its data."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

PAPER_ADDED = "paper.added"
PAPER_REVISED = "paper.revised"
VERDICT_RECORDED = "verdict.recorded"
# A paper's effective grade for a Watch topic changed, by a verdict or the owner's word.
GRADE_CHANGED = "grade.changed"
PAPER_SAVED = "paper.saved"
PAPER_UNSAVED = "paper.unsaved"
# A take was written for (paper, topic); it may arrive after `paper.saved`.
TAKE_WRITTEN = "take.written"
# A published record was found for a paper, or the owner set one.
PUBLICATION_FOUND = "publication.found"


async def emit(
    conn: psycopg.AsyncConnection, type: str, paper_id: str | None = None, **payload: Any
) -> None:
    """Record an event. Call it inside the transaction that writes what it describes."""
    await conn.execute(
        "INSERT INTO outbox (type, paper_id, payload) VALUES (%s, %s, %s)",
        (type, paper_id, Jsonb(payload)),
    )
