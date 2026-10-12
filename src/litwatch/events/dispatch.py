"""The dispatcher: hands outbox events to each loaded integration, in id order, from that
integration's own cursor.

Each integration is delivered to on its own connection and its own loop, so one that fails
or takes long (a deep card can take most of an hour) delays nobody else. When `handle`
raises, the cursor stays before that event, the failure is recorded for the API, and the
event is tried again after a backoff that doubles up to an hour; the integration's later
events wait behind it, since they may depend on it. The owner can retry the event at once
or skip it. A restart neither loses an event nor hands over one already done, apart from
the one in hand when it stopped, which is why `handle` must be idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import store
from litwatch.events import Integration
from litwatch.store import outbox

log = logging.getLogger(__name__)

BATCH = 100
IDLE_SECONDS = 5.0
# Held per integration while delivering, so two services never hand over one event twice.
DISPATCH_LOCK = 7213914


def backoff(failures: int) -> timedelta:
    """30 seconds after the first failure, doubling, at most an hour."""
    return timedelta(seconds=min(3600, 30 * 2 ** min(max(failures - 1, 0), 7)))


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


async def deliver(
    conn: psycopg.AsyncConnection, integration: Integration, now: datetime | None = None
) -> int:
    """One pass: hand over the events after the cursor, up to a batch, stopping at the first
    failure. Returns how many events the cursor moved past, wanted or not."""
    now = now or datetime.now(UTC)
    key = (DISPATCH_LOCK, integration.name)
    row = await (
        await conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS locked", key)
    ).fetchone()
    if not row["locked"]:
        return 0
    try:
        cursor = await outbox.cursor(conn, integration.name)
        if cursor.retry_at and cursor.retry_at > now:
            return 0
        position, passed = cursor.position, 0
        for event in await outbox.after(conn, cursor.position, BATCH):
            if integration.wants(event):
                try:
                    await integration.handle(conn, event)
                except Exception as exc:
                    await outbox.advance(conn, integration.name, position)
                    failures = cursor.failures + 1 if cursor.failing == event.id else 1
                    await outbox.failed(
                        conn,
                        integration.name,
                        event.id,
                        failures,
                        _describe(exc),
                        now + backoff(failures),
                    )
                    log.warning(
                        "%s: event %s (%s) failed %d time(s): %s",
                        integration.name,
                        event.id,
                        event.type,
                        failures,
                        _describe(exc),
                    )
                    return passed
            position, passed = event.id, passed + 1
        await outbox.advance(conn, integration.name, position)
        return passed
    finally:
        await conn.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", key)


async def start(conn: psycopg.AsyncConnection, integration: Integration) -> None:
    """Make the integration's cursor (at the outbox's end, the first time) and run its setup."""
    await outbox.cursor(conn, integration.name)
    if setup := getattr(integration, "setup", None):
        await setup(conn)


async def _loop(dsn: str, integration: Integration) -> None:
    started = False
    while True:
        try:
            async with await store.connect(dsn) as conn:
                if not started:
                    await start(conn, integration)
                    started = True
                while True:
                    if not await deliver(conn, integration):
                        await asyncio.sleep(IDLE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A lost database or a failing setup: try again shortly, as the scheduler does.
            log.exception("%s: dispatcher pass failed", integration.name)
            await asyncio.sleep(IDLE_SECONDS)


async def run_forever(dsn: str, integrations: list[Integration]) -> None:
    """Deliver to every integration until cancelled, each in a loop of its own."""
    log.info("dispatcher on: %s", ", ".join(i.name for i in integrations))
    tasks = [asyncio.create_task(_loop(dsn, i), name=f"dispatch:{i.name}") for i in integrations]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for integration in integrations:
            if close := getattr(integration, "aclose", None):
                with contextlib.suppress(Exception):
                    await close()
