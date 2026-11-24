"""The Workers of full-text and Deep passes (docs/plans/a2-work-gate.md, P2): each work item of
kind `fulltext` or `deep` is one try of one pass of the regular queue, keyed by the pass's id
(`store.fulltext`, which submits them).

An item claims its pass as every worker does, with the pass's own lease, and works it as the
queue's workers did: a text pass as a wave of one on a `fulltext` run of its own, a Deep pass
as one job on the deep service. A pass it cannot claim says what follows:
- one another owner holds (the nightly inline, or a process gone whose claim has not lapsed):
  the item waits until that claim's lease ends, and then takes it over or finds it done;
- one held back while a nightly runs (an automatic text pass, which the nightly batches) or is
  due (a re-run's Deep pass), or while the deep service is paused: the item waits and looks
  again;
- one done, gone, or no longer the regular queue's: there is nothing to do.

A pass awaiting its text is tried again after `TEXT_WAIT`, and a Deep pass the service failed
after its pause, as the same item. An item whose try raised (but for a lost database, which the
gate tries again) is failed, and its pass's next try submitted for when its claim lapses."""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from litwatch import fulltext
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core.fulltext import LEASE, TEXT_WAIT, PassState
from litwatch.core.work import WorkItem
from litwatch.feedback import word
from litwatch.fulltext import deep as deep_passes
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import Later, database_lost
from litwatch.integrations.deep import Deep
from litwatch.jobs.context import GatewayFactory, default_gateway
from litwatch.jobs.worker import nightly_waits
from litwatch.store import fulltext as store_
from litwatch.store import integrations as cards
from litwatch.store import work
from litwatch.store.fulltext import Claimed


def _unclaimed(standing: tuple[PassState, datetime | None] | None, held_back: bool) -> Later | None:
    """What follows for an item whose pass was not claimed, as it stands (`store.fulltext.
    standing`): the end of another owner's claim; another look after `TEXT_WAIT` when a claim
    could take it but for what holds it back (`held_back`); else nothing to do."""
    if standing is None:
        return None
    state, held_until = standing
    if held_until is not None:
        return Later(held_until)
    if held_back and state in (PassState.READY, PassState.AWAITING_TEXT, PassState.RUNNING):
        return Later(datetime.now(UTC) + TEXT_WAIT)
    return None


@asynccontextmanager
async def _tried_again(db: DatabaseGate, item: WorkItem, c: Claimed) -> AsyncGenerator[None]:
    """Submit the pass's next try for when its claim lapses if the block raises, but for a lost
    database, with which the gate tries the item again itself."""
    try:
        yield
    except Exception as exc:
        if not database_lost(exc):
            async with db.write(f"{item.kind} pass: tried again") as unit:
                await work.submit(unit, item.kind, item.key, not_before=c.lease_until)
        raise


class TextPassWorker:
    """Works text passes, reading their texts from `archive`, through `gateway`'s models."""

    def __init__(
        self,
        db: DatabaseGate,
        config: Config,
        archive: ArchiveClient,
        gateway: GatewayFactory = default_gateway,
    ) -> None:
        self.db, self.config, self.archive, self.gateway = db, config, archive, gateway

    async def sweep(self) -> None:
        """Submit the item of each text pass a claim can take."""
        async with self.db.write("text passes: sweep") as unit:
            await store_.submit_open(unit)

    async def __call__(self, item: WorkItem) -> Later | None:
        # While another job holds the run lock (a nightly batching the others), the owner's
        # alone (models.md, batches).
        owners = True if await self.db.held_elsewhere("run") else None
        async with self.db.write("claim a text pass") as unit:
            claimed = await store_.claim(unit, LEASE, 1, owners=owners, pass_id=int(item.key))
            standing = None if claimed else await store_.standing(unit, int(item.key))
        if not claimed:
            return _unclaimed(standing, held_back=owners is True)
        async with _tried_again(self.db, item, claimed[0]):
            done = await fulltext.wave(
                self.db,
                self.config,
                self.archive,
                claimed,
                run=None,
                gateway=self.gateway,
                owners=owners,
            )
        return _awaiting(done)


class DeepPassWorker:
    """Works Deep passes on the deep service `deep`, reading their texts from `archive`."""

    def __init__(
        self, db: DatabaseGate, config: Config, archive: ArchiveClient, deep: Deep
    ) -> None:
        self.db, self.config, self.archive, self.deep = db, config, archive, deep

    async def sweep(self) -> None:
        """Submit the item of each Deep pass a claim can take."""
        async with self.db.write("deep passes: sweep") as unit:
            await store_.submit_open(unit, deep=True)

    async def __call__(self, item: WorkItem) -> Later | None:
        if until := await deep_passes.paused(self.db):
            return Later(until)
        now = datetime.now(self.config.schedule.zone)
        background = not await nightly_waits(self.db, self.config, now)
        async with self.db.write("claim a deep pass") as unit:
            claimed = await store_.claim(
                unit, self.deep.lease, 1, deep=True, background=background, pass_id=int(item.key)
            )
            # A pass with an attempt open has started: its job is followed whatever the paper
            # wants.
            attempt = await cards.open_attempt(unit, claimed[0].id) if claimed else None
            standing = None if claimed else await store_.standing(unit, int(item.key))
        if not claimed:
            return _unclaimed(standing, held_back=not background)
        (c,) = claimed
        async with _tried_again(self.db, item, c):
            why = await deep_passes.work_one(
                self.db, self.archive, self.deep, c, attempt, word.rules(self.config)
            )
        if why == "card_service_failed":
            return Later(datetime.now(UTC) + deep_passes.PAUSE)
        return _awaiting(Counter([why]))


def _awaiting(done: Counter[str]) -> Later | None:
    """A try that left its pass awaiting its text is tried again after `TEXT_WAIT`."""
    return Later(datetime.now(UTC) + TEXT_WAIT) if done["awaiting_text"] else None
