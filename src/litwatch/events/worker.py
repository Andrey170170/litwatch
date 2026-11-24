"""The delivery Worker (docs/plans/a2-work-gate.md, P2): each work item of kind `deliver` is one
turn of one integration's delivery (`events.dispatch`), keyed by its name. A process serves the
keys of the integrations it loads alone (`keys`, which `serve` gives `WorkGate.run`): a turn for
one it does not load waits for a process that does.

A turn sets the integration up, the first time in this process, then delivers one batch; one
that hands nothing over calls the integration's `idle` hook, at most once every
`dispatch.IDLE_SECONDS`. Then the turn arranges the next itself, and alone: under the cursor's
row lock, which every write that schedules a turn takes first (`store.outbox`), it forgets what
was submitted for it while it ran (`store.work.forget_resubmission`), reads the cursor and the
outbox, and submits its own key, which runs once this turn ends, at the sooner of two times:
when events may be delivered (at once while settled events wait, a little later while only
events an older transaction holds back wait, `RETRY`, when a backoff ends while a failure or a
failed setup waits one out), and, for an integration with an `idle` hook, when it is due again.
With nothing left, the next event's commit wakes it (`store.outbox.emit`), bringing a turn
that waits for the hook forward but none past the backoff it reads; the owner's retry or skip
wakes it at once."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from litwatch.core.work import WorkItem
from litwatch.events import Reacting, dispatch
from litwatch.gates.database import DatabaseGate
from litwatch.store import outbox, work

# How soon a turn comes again for events that committed behind an older transaction still
# running: the outbox reads them once it ends.
RETRY = timedelta(seconds=5)


class DeliveryWorker:
    """Delivers to the integrations `integrations`, through the gate `db`."""

    def __init__(self, db: DatabaseGate, integrations: list[Reacting]) -> None:
        self.db = db
        self.loaded = {i.name: i for i in integrations}
        self.set_up: set[str] = set()  # those whose setup ran in this process
        self.idled: dict[str, float] = {}  # when each one's `idle` last ran, on the loop's clock

    @property
    def keys(self) -> list[str]:
        """The keys this Worker serves: the integrations it loads."""
        return sorted(self.loaded)

    async def sweep(self) -> None:
        """Submit a turn for each integration loaded: its setup, and what waits for it."""
        async with self.db.write("deliveries: sweep") as unit:
            for name in self.keys:
                await outbox.wake(unit, name)

    async def __call__(self, item: WorkItem) -> None:
        integration = self.loaded[item.key]
        if item.key not in self.set_up and await dispatch.start(self.db, integration):
            self.set_up.add(item.key)
        idles = getattr(integration, "idle", None) is not None
        if item.key in self.set_up:
            passed = await dispatch.deliver(self.db, integration)
            if not passed and idles and self._idle_due(item.key):
                await dispatch.idle(self.db, integration)
        async with self.db.write(f"dispatch {item.key}: next turn") as unit:
            cursor = await outbox.cursor(unit, item.key, lock=True)
            await work.forget_resubmission(unit, item)
            settled, waiting = await outbox.waiting(unit, item.key)
            now = datetime.now(UTC)
            if item.key not in self.set_up:
                times = [cursor.setup_retry_at or now + RETRY]
            else:
                times = [self._idle_at(item.key, now)] if idles else []
                if cursor.failing is not None and cursor.retry_at:
                    times.append(cursor.retry_at)
                elif settled:
                    times.append(now)
                elif waiting:
                    times.append(now + RETRY)
            if times:
                await outbox.wake(unit, item.key, at=min(times))

    def _idle_at(self, name: str, now: datetime) -> datetime:
        """When the integration's `idle` is due again: `dispatch.IDLE_SECONDS` after it last
        ran, now if it never has."""
        last = self.idled.get(name)
        if last is None:
            return now
        since = asyncio.get_running_loop().time() - last
        return now + timedelta(seconds=max(0.0, dispatch.IDLE_SECONDS - since))

    def _idle_due(self, name: str) -> bool:
        """Whether the integration's `idle` may run now, counting it as run if so."""
        clock = asyncio.get_running_loop().time()
        last = self.idled.get(name)
        if last is not None and clock - last < dispatch.IDLE_SECONDS:
            return False
        self.idled[name] = clock
        return True
