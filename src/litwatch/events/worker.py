"""The delivery Worker (docs/plans/a2-work-gate.md, P2): each work item of kind `deliver` is one
turn of one integration's delivery (`events.dispatch`), keyed by its name. A process serves the
keys of the integrations it loads alone (`keys`, which `serve` gives `WorkGate.run`, none when
none reacts to events): an event wakes only those (`store.outbox.wake_all`), and the gate's
take-back ends a turn of any other (`store.work.UNSERVED`), whose cursor keeps its place.

A turn sets the integration up, the first time in this process, then delivers one batch; one
that hands nothing over calls the integration's `idle` hook, at most once every
`dispatch.IDLE_SECONDS`. Then the turn arranges the next itself, and alone: under the cursor's
row lock, which every write that schedules a turn takes first (`store.outbox`), it forgets what
was submitted for it while it ran (`store.work.forget_resubmission`), reads the cursor and the
outbox, and submits its own key, which runs once this turn ends (`_arrange`). While the
integration is not set up in this process, that is when its setup may run, which holds back the
rest; after, the sooner of two times: when events may be delivered (at once while settled events
wait, a little later while only events an older transaction holds back wait, `RETRY`, when a
backoff ends while a failure waits one out), and, for an integration with an `idle` hook, when
it is due again. The startup sweep arranges each first turn the same way, so an event's backoff
holds back neither setup nor `idle`. With nothing left, the next event's commit wakes it
(`store.outbox.emit`), bringing a turn that waits for the hook forward but none past the backoff
it reads; the owner's retry or skip wakes it at once."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from litwatch.core.work import WorkItem
from litwatch.events import Reacting, dispatch
from litwatch.gates.database import DatabaseGate, WriteUnit
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
        """Submit a turn for each integration loaded, as a turn's end would (`_arrange`): its
        setup comes first, so at once, or when a failed setup's backoff ends; an event's
        backoff holds back no setup and no `idle` hook. One with no setup first clears a setup
        failure the name's last implementation recorded (`store.outbox.setup_done`, which
        locks the cursor). One unit, the cursors locked in name order as an emitter's wake
        locks them; it makes none (the first turn does), so it waits for no emitter."""
        async with self.db.write("deliveries: sweep") as unit:
            for name in self.keys:
                if getattr(self.loaded[name], "setup", None) is None:
                    await outbox.setup_done(unit, name)
                await self._arrange(unit, name)

    async def __call__(self, item: WorkItem) -> None:
        integration = self.loaded[item.key]
        if item.key in self.set_up:
            # A setup that failed since in another process serving it undoes this one's, before
            # anything is delivered on it: it runs again here too, after the failure's backoff.
            async with self.db.read(f"dispatch {item.key}: set up elsewhere") as unit:
                if await outbox.setup_failing(unit, item.key):
                    self.set_up.discard(item.key)
        if item.key not in self.set_up and await dispatch.start(self.db, integration):
            self.set_up.add(item.key)
        if item.key in self.set_up:
            passed = await dispatch.deliver(self.db, integration)
            if not passed and self._idles(item.key) and self._idle_due(item.key):
                await dispatch.idle(self.db, integration)
                # Counted once it returns: a conflict raised from it leaves it due for the retry.
                self.idled[item.key] = asyncio.get_running_loop().time()
        async with self.db.write(f"dispatch {item.key}: next turn") as unit:
            await self._arrange(unit, item.key, item)

    async def _arrange(self, unit: WriteUnit, name: str, turn: WorkItem | None = None) -> None:
        """Submit the integration's next turn, under its cursor's row lock: for `turn`, the
        one ending, after forgetting what was submitted for it while it ran. Its time is the
        sooner of when its setup may run, if it has not in this process (it holds back the
        rest), or else when events may be delivered and when its `idle` is due. A setup that
        failed since in another process serving it, recorded on the cursor, undoes this one's
        (as the turn's start reads it too): it runs again here too, after the failure's backoff.
        One with no cursor yet has its first turn now, which makes it (`dispatch.start`)."""
        cursor = await outbox.locked(unit, name)
        if turn is not None:
            await work.forget_resubmission(unit, turn)
        settled, waiting = await outbox.waiting(unit, name)
        now = datetime.now(UTC)
        if cursor is not None and cursor.setup_failures:
            self.set_up.discard(name)
        if cursor is None:
            times = [now]
        elif name not in self.set_up:  # after its backoff, or at once on the owner's Retry
            times = [(cursor.setup_retry_at if cursor.setup_failures else None) or now]
        else:
            times = [self._idle_at(name, now)] if self._idles(name) else []
            if cursor.failing is not None and cursor.retry_at:
                times.append(cursor.retry_at)
            elif settled:
                times.append(now)
            elif waiting:
                times.append(now + RETRY)
        if times:
            await outbox.wake(unit, name, at=min(times))

    def _idles(self, name: str) -> bool:
        return getattr(self.loaded[name], "idle", None) is not None

    def _idle_at(self, name: str, now: datetime) -> datetime:
        """When the integration's `idle` is due again: `dispatch.IDLE_SECONDS` after it last
        ran, now if it never has."""
        last = self.idled.get(name)
        if last is None:
            return now
        since = asyncio.get_running_loop().time() - last
        return now + timedelta(seconds=max(0.0, dispatch.IDLE_SECONDS - since))

    def _idle_due(self, name: str) -> bool:
        """Whether the integration's `idle` may run now."""
        last = self.idled.get(name)
        return last is None or asyncio.get_running_loop().time() - last >= dispatch.IDLE_SECONDS
