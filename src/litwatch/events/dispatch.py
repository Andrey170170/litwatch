"""The dispatcher: hands outbox events to each loaded integration that reacts to them
(`events.Reacting`), in id order, from that integration's own cursor.

Each integration is delivered to in its own loop, under its own dispatch lock, so one that
fails or takes long (a WebDAV upload of a large PDF) delays nobody else. The dispatcher's
bookkeeping (the cursor, a failure) is in units of its own; an integration's `handle`, `setup`
and `idle` are given the gate and open their own, outside the dispatcher's. When `handle`
raises, the cursor stays before that event, the failure is recorded for the API, and the
event is tried again after a backoff that doubles up to an hour; the integration's later
events wait behind it, since they may depend on it. The owner can retry the event at once
or skip it. A restart neither loses an event nor hands over one already done, apart from
the one in hand when it stopped, which is why `handle` must be idempotent.

An integration's `setup` runs when its loop starts, under the same lock, before any event is
handed over. A failing setup is recorded for the API like a failing event, with no event
attached, and tried again after the same backoff; the integration's events wait meanwhile.

An integration with work of its own that no event brings (a delivery waiting on something
outside litwatch) gets an `idle(db)` call when it has nothing to be handed, at most once a minute,
under the same lock. A failing `idle` is logged and changes nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

from litwatch.core import Event
from litwatch.events import Reacting
from litwatch.gates.database import BusyError, DatabaseGate
from litwatch.store import outbox

log = logging.getLogger(__name__)

BATCH = 100
IDLE_SECONDS = 5.0
IDLE_HOOK_SECONDS = 60.0  # the least time between two `idle` calls to one integration


def backoff(failures: int) -> timedelta:
    """30 seconds after the first failure, doubling, at most an hour."""
    return timedelta(seconds=min(3600, 30 * 2 ** min(max(failures - 1, 0), 7)))


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _clock(now: datetime | None) -> Callable[[], datetime]:
    return (lambda: now) if now else (lambda: datetime.now(UTC))


async def _next(
    db: DatabaseGate, name: str, clock: Callable[[], datetime]
) -> tuple[outbox.Cursor, list[Event]] | None:
    """The integration's cursor and the settled events after it, up to a batch; None while a
    failure waits out its backoff."""
    async with db.write(f"dispatch {name}: next") as unit:
        cursor = await outbox.cursor(unit, name)
        if cursor.retry_at and cursor.retry_at > clock():
            return None
        return cursor, await outbox.after(unit, cursor.position, BATCH)


@contextlib.asynccontextmanager
async def _dispatching(db: DatabaseGate, name: str) -> AsyncIterator[bool]:
    """Hold the integration's dispatch lock for the block, if no other session holds it; yields
    whether it is held."""
    held = contextlib.AsyncExitStack()
    try:
        await held.enter_async_context(db.held("dispatch", name))
    except BusyError:
        yield False
        return
    async with held:
        yield True


async def deliver(db: DatabaseGate, integration: Reacting, now: datetime | None = None) -> int:
    """One pass: hand over the settled events after the cursor, up to a batch, stopping at the
    first failure. Returns how many events the cursor moved past, wanted or not. `now` fixes
    the clock, for tests; otherwise a failure's backoff runs from the moment it failed. With
    nothing to hand over it takes no lock."""
    clock, name = _clock(now), integration.name
    if not (found := await _next(db, name, clock)) or not found[1]:
        return 0
    async with _dispatching(db, name) as locked:
        if not locked or not (found := await _next(db, name, clock)):
            return 0
        cursor, todo = found
        position, passed = cursor.position, 0
        for event in todo:
            if integration.wants(event):
                try:
                    await integration.handle(db, event)
                except Exception as exc:
                    failures = cursor.failures + 1 if cursor.failing == event.id else 1
                    at = clock()
                    async with db.write(f"dispatch {name}: failed") as unit:
                        await outbox.advance(unit, name, position)
                        recorded = await outbox.failed(
                            unit, name, event, failures, _describe(exc), at, at + backoff(failures)
                        )
                    log.warning(
                        "%s: event %s (%s) failed %d time(s)%s: %s",
                        name,
                        event.id,
                        event.type,
                        failures,
                        "" if recorded else ", but the owner skipped it meanwhile",
                        _describe(exc),
                    )
                    return passed
            position, passed = outbox.Position.of(event), passed + 1
        async with db.write(f"dispatch {name}: advance") as unit:
            await outbox.advance(unit, name, position)
        return passed


async def start(db: DatabaseGate, integration: Reacting, now: datetime | None = None) -> bool:
    """Make the integration's cursor (at the last settled event, the first time) and run its
    setup under the dispatch lock. Returns whether the integration is set up: False while
    another session holds the lock, while a failed setup waits out its backoff, and after a
    setup that failed, which is recorded. `now` fixes the clock, for tests."""
    clock, name = _clock(now), integration.name
    setup = getattr(integration, "setup", None)
    async with db.write(f"dispatch {name}: cursor") as unit:
        await outbox.cursor(unit, name)
        if setup is None:
            # The section may have had another implementation, one whose setup failed.
            await outbox.setup_done(unit, name)
            return True
    async with _dispatching(db, name) as locked:
        if not locked:
            return False
        async with db.write(f"dispatch {name}: setup due") as unit:
            cursor = await outbox.cursor(unit, name)
        if cursor.setup_failures and cursor.setup_retry_at and cursor.setup_retry_at > clock():
            return False
        try:
            await setup(db)
        except Exception as exc:
            failures, at = cursor.setup_failures + 1, clock()
            async with db.write(f"dispatch {name}: setup failed") as unit:
                await outbox.setup_failed(
                    unit, name, failures, _describe(exc), at, at + backoff(failures)
                )
            log.warning("%s: setup failed %d time(s): %s", name, failures, _describe(exc))
            return False
        async with db.write(f"dispatch {name}: set up") as unit:
            await outbox.setup_done(unit, name)
        return True


async def idle(db: DatabaseGate, integration: Reacting) -> bool:
    """Call the integration's `idle` hook, if it has one, under its delivery lock. A failure is
    logged and goes no further. Returns whether the hook ran."""
    hook = getattr(integration, "idle", None)
    if hook is None:
        return False
    async with _dispatching(db, integration.name) as locked:
        if not locked:
            return False
        try:
            await hook(db)
        except Exception:
            log.exception("%s: idle work failed", integration.name)
    return True


async def _loop(db: DatabaseGate, integration: Reacting) -> None:
    started = False
    clock = asyncio.get_running_loop().time
    idled = clock() - IDLE_HOOK_SECONDS
    while True:
        try:
            while not started:
                if not (started := await start(db, integration)):
                    await asyncio.sleep(IDLE_SECONDS)
            while True:
                if not await deliver(db, integration):
                    if clock() - idled >= IDLE_HOOK_SECONDS:
                        idled = clock()
                        await idle(db, integration)
                    await asyncio.sleep(IDLE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A lost database: try again shortly, as the scheduler does.
            log.exception("%s: dispatcher pass failed", integration.name)
            await asyncio.sleep(IDLE_SECONDS)


async def run_forever(db: DatabaseGate, integrations: list[Reacting]) -> None:
    """Deliver to every integration until cancelled, each in a loop of its own. Closing them is
    their owner's (the app's), once everything that uses them has stopped."""
    log.info("dispatcher on: %s", ", ".join(i.name for i in integrations))
    tasks = [asyncio.create_task(_loop(db, i), name=f"dispatch:{i.name}") for i in integrations]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
