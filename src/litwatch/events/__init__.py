"""The outbox and the integration contract. A pipeline step writes its event in the same
transaction as its data; the dispatcher (`events.dispatch`) hands events to integrations."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Event

PAPER_ADDED = "paper.added"
PAPER_REVISED = "paper.revised"
VERDICT_RECORDED = "verdict.recorded"
# A paper's effective grade for a Watch topic changed, by a verdict or the owner's word.
GRADE_CHANGED = "grade.changed"
PAPER_SAVED = "paper.saved"
PAPER_UNSAVED = "paper.unsaved"
# A published record was found for a paper, or the owner set one.
PUBLICATION_FOUND = "publication.found"
# The owner asked for a deep card by hand, whatever the paper's grade.
CARD_REQUESTED = "card.requested"
# A deep card was stored for a paper (emitted by the `deep` integration).
CARD_READY = "card.ready"


async def emit(
    conn: psycopg.AsyncConnection, type: str, paper_id: str | None = None, **payload: Any
) -> int:
    """Record an event. Call it inside the transaction that writes what it describes.
    Returns the event's id."""
    row = await (
        await conn.execute(
            "INSERT INTO outbox (type, paper_id, payload) VALUES (%s, %s, %s) RETURNING id",
            (type, paper_id, Jsonb(payload)),
        )
    ).fetchone()
    return row["id"]


@runtime_checkable
class Integration(Protocol):
    """Something outside the core that reacts to events: Zotero, a webhook, a deep card.

    `wants` is a cheap filter on the event alone; `handle` does the work and must be
    idempotent, because an event is handed over again after a failure or a restart. It
    reads what it needs through `store`, keeps its own state in its own table, and may
    emit events of its own on the connection it is given. Raising tells the dispatcher to
    retry the event later; the integration's later events wait behind it, nobody else's do.

    Optional hooks, used when present: `setup(conn)`, awaited once whenever the dispatcher
    starts; `aclose()`, awaited when it stops; `requires`, names of integrations that must
    be loaded too; `makes_cards = True` for one that answers `card.requested` by storing a
    deep card, which is what makes the paper page offer the request; `idle(conn)`, awaited
    when it has no event to be handed, at most about once a minute, for work no event brings
    (a failure there is logged and changes nothing else).
    """

    name: str
    description: str

    def wants(self, event: Event) -> bool: ...

    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None: ...
