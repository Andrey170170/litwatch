"""Delivery: hands outbox events to an integration that reacts to them (`events.Reacting`), in
id order, from that integration's own cursor. Each call is one turn of the integration's
delivery work item (`events.worker`), whose admission lets one turn per integration run at a
time, so one that fails or takes long (a WebDAV upload of a large PDF) delays nobody else.

The bookkeeping (the cursor, a failure) is in units of its own; an integration's `handle` and
`setup` are given the gate and open their own. When `wants` or `handle` raises, the cursor stays
before that event, the failure is recorded for the API, and the event is tried again after a
backoff that doubles up to an hour; the integration's later events wait behind it, since they
may depend on it. The owner can retry the event at once or skip it. A lost database, or a
statement that lost a deadlock or a serialization conflict, is no failure of the
integration's: it is raised, and the turn is tried again. A restart neither loses
an event nor hands over one already done, apart from the one in hand when it stopped, which is
why `handle` must be idempotent.

An integration's `setup` runs before its first event in each process that delivers to it. A
failing setup is recorded for the API like a failing event, with no event attached, and tried
again after the same backoff; the integration's events wait meanwhile. One with an `idle(db)`
hook (for work no event brings, outside litwatch) has it called on a turn that hands nothing
over, at most once every `IDLE_SECONDS`; a failing `idle` is logged and changes nothing else.
A failing `setup` or `idle` that is the database's is raised too.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from litwatch.events import Reacting
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import conflicted, database_lost
from litwatch.store import outbox

log = logging.getLogger(__name__)

BATCH = 100
IDLE_SECONDS = 60.0  # the least time between two `idle` calls to one integration


def backoff(failures: int) -> timedelta:
    """30 seconds after the first failure, doubling, at most an hour."""
    return timedelta(seconds=min(3600, 30 * 2 ** min(max(failures - 1, 0), 7)))


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _not_theirs(exc: BaseException) -> bool:
    """Whether `exc` is no failure of the integration's but the database's: it went away, or
    a statement lost a deadlock or a serialization conflict. It is raised, and the gate tries
    the turn again."""
    return database_lost(exc) or conflicted(exc)


def _clock(now: datetime | None) -> Callable[[], datetime]:
    return (lambda: now) if now else (lambda: datetime.now(UTC))


async def deliver(db: DatabaseGate, integration: Reacting, now: datetime | None = None) -> int:
    """One turn: hand over the settled events after the cursor, up to a batch, stopping at the
    first failure. Returns how many events the cursor moved past, wanted or not. `now` fixes
    the clock, for tests; otherwise a failure's backoff runs from the moment it failed."""
    clock, name = _clock(now), integration.name
    async with db.write(f"dispatch {name}: next") as unit:
        cursor = await outbox.cursor(unit, name)
        if cursor.retry_at and cursor.retry_at > clock():
            return 0
        todo = await outbox.after(unit, cursor.position, BATCH)
    position, passed = cursor.position, 0
    for event in todo:
        try:
            if integration.wants(event):
                await integration.handle(db, event)
        except Exception as exc:
            if _not_theirs(exc):
                raise
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
    if passed:
        async with db.write(f"dispatch {name}: advance") as unit:
            await outbox.advance(unit, name, position)
    return passed


async def idle(db: DatabaseGate, integration: Reacting) -> None:
    """Call the integration's `idle` hook, if it has one. A failure is logged and goes no
    further, but for the database's own (`_not_theirs`), which is raised."""
    hook = getattr(integration, "idle", None)
    if hook is None:
        return
    try:
        await hook(db)
    except Exception as exc:
        if _not_theirs(exc):
            raise
        log.exception("%s: idle work failed", integration.name)


async def start(db: DatabaseGate, integration: Reacting, now: datetime | None = None) -> bool:
    """Make the integration's cursor (at the last settled event, the first time) and run its
    setup. Returns whether the integration is set up: False while a failed setup waits out its
    backoff, and after a setup that failed, which is recorded. `now` fixes the clock, for
    tests."""
    clock, name = _clock(now), integration.name
    setup = getattr(integration, "setup", None)
    async with db.write(f"dispatch {name}: cursor") as unit:
        cursor = await outbox.cursor(unit, name)
        if setup is None:
            # The section may have had another implementation, one whose setup failed.
            await outbox.setup_done(unit, name)
            return True
    if cursor.setup_failures and cursor.setup_retry_at and cursor.setup_retry_at > clock():
        return False
    try:
        await setup(db)
    except Exception as exc:
        if _not_theirs(exc):
            raise
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
