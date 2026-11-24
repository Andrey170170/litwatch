"""The delivery Worker (docs/plans/a2-work-gate.md, P2): each work item of kind `deliver` is one
turn of one integration's delivery (`events.dispatch`), keyed by its name.

A turn sets the integration up, the first time in this process, then delivers one batch. While
it runs it arranges the next turn, by submitting its own key, which runs once this one ends: at
once while settled events wait, a little later while only events an older transaction holds
back wait (`RETRY`), and when a backoff ends while a failure or a failed setup waits one out.
With nothing left, the next event's commit wakes it (`store.outbox.emit`), as the owner's retry
or skip does. An integration this process does not load waits for one that does: a process
submits each integration it loads as it starts (`sweep`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from litwatch.core.work import WorkItem
from litwatch.events import Reacting, dispatch
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import Later
from litwatch.store import outbox

# How soon a turn comes again for events that committed behind an older transaction still
# running: the outbox reads them once it ends.
RETRY = timedelta(seconds=5)
# When an item for an integration this process does not load runs: not until one that does
# submits it.
NEVER = datetime.max.replace(tzinfo=UTC)


class DeliveryWorker:
    """Delivers to the integrations `integrations`, through the gate `db`."""

    def __init__(self, db: DatabaseGate, integrations: list[Reacting]) -> None:
        self.db = db
        self.loaded = {i.name: i for i in integrations}
        self.set_up: set[str] = set()  # those whose setup ran in this process

    async def sweep(self) -> None:
        """Submit a turn for each integration loaded: its setup, and what waits for it."""
        async with self.db.write("deliveries: sweep") as unit:
            for name in sorted(self.loaded):
                await outbox.wake(unit, name)

    async def __call__(self, item: WorkItem) -> Later | None:
        integration = self.loaded.get(item.key)
        if integration is None:
            return Later(NEVER)
        if item.key not in self.set_up and await dispatch.start(self.db, integration):
            self.set_up.add(item.key)
        if item.key in self.set_up:
            await dispatch.deliver(self.db, integration)
        async with self.db.write(f"dispatch {item.key}: next turn") as unit:
            cursor = await outbox.cursor(unit, item.key)
            settled, waiting = await outbox.waiting(unit, item.key)
            now = datetime.now(UTC)
            if item.key not in self.set_up:
                at = cursor.setup_retry_at or now + RETRY
            elif cursor.failing is not None and cursor.retry_at:
                at = cursor.retry_at
            elif settled:
                at = now
            elif waiting:
                at = now + RETRY
            else:
                return None
            await outbox.wake(unit, item.key, at=at)
        return None
