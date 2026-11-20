"""The integration contract and the event types. A pipeline step writes its event in the same
unit as its data (`store.outbox.emit`); the dispatcher (`events.dispatch`) hands events to
integrations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from litwatch.core import Event
from litwatch.core.events import (
    BRIEF_WRITTEN,
    CARD_READY,
    CARD_REQUESTED,
    DIGEST_WRITTEN,
    GIST_WRITTEN,
    GRADE_CHANGED,
    PAPER_ADDED,
    PAPER_CLASSIFIED,
    PAPER_REVISED,
    PAPER_SAVED,
    PAPER_UNSAVED,
    PUBLICATION_FOUND,
    VERDICT_RECORDED,
)
from litwatch.gates.database import DatabaseGate

# The event types, defined in `core` so `store` writes them too; named here for integrations.
__all__ = [
    "BRIEF_WRITTEN",
    "CARD_READY",
    "CARD_REQUESTED",
    "DIGEST_WRITTEN",
    "GIST_WRITTEN",
    "GRADE_CHANGED",
    "PAPER_ADDED",
    "PAPER_CLASSIFIED",
    "PAPER_REVISED",
    "PAPER_SAVED",
    "PAPER_UNSAVED",
    "PUBLICATION_FOUND",
    "VERDICT_RECORDED",
    "Integration",
    "Reacting",
    "reacting",
]


@runtime_checkable
class Integration(Protocol):
    """Something outside the core that the configuration loads: Zotero, a webhook, the deep
    service. Most react to events (`Reacting`); one that reacts to none (deep, whose work is the
    full-text queue's) is never handed one and keeps no cursor.

    Optional hooks, used when present: `aclose()`, awaited when the service stops; `requires`,
    names of integrations that must be loaded too.
    """

    name: str
    description: str


@runtime_checkable
class Reacting(Integration, Protocol):
    """An integration the dispatcher hands events to.

    `wants` is a cheap filter on the event alone; `handle` does the work and must be
    idempotent, because an event is handed over again after a failure or a restart. It is
    given the Database gate, `db`, and opens its own units on it: it reads what it needs
    through `store` in read units, keeps its own state in its own table, and may emit events
    of its own (`store.outbox.emit`), each write in a write unit, and makes its network calls
    with no unit open. Raising tells the dispatcher to retry the event later; the
    integration's later events wait behind it, nobody else's do.

    Optional hooks, used when present: `setup(db)`, awaited once whenever the dispatcher
    starts, before any event and under the same lock as `handle` (a failure is shown and tried
    again after a backoff, and events wait for it); `idle(db)`, awaited when it has no event
    to be handed, at most about once a minute, for work no event brings (a failure there is
    logged and changes nothing else).
    """

    def wants(self, event: Event) -> bool: ...

    async def handle(self, db: DatabaseGate, event: Event) -> None: ...


def reacting(integrations: list[Integration]) -> list[Reacting]:
    """The integrations that react to events: the ones the dispatcher delivers to, and the only
    ones with a cursor to show."""
    return [i for i in integrations if isinstance(i, Reacting)]
