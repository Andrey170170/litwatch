"""The Work gate (docs/specs/architecture/a2-work-gate.md; docs/plans/a2-work-gate.md, P1): it
admits work items, leases them to this process's Workers, renews and takes back their leases,
and records how each ended.

    gate = WorkGate(db)
    await gate.run({"fulltext": fulltext_worker, "deliver": deliver_worker})   # until cancelled

Work is submitted elsewhere, by `store.work.submit` in the submitter's own unit. `run` takes
what is due, one item at a time per admission (a kind, or an integration for deliveries), and
runs each in a task of its own under the work context (`gates.context`): the item's lease
fences that task's write units (`gates.database`), and its gate calls are attributed to the
item. A heartbeat renews the lease every third of it; one that finds the lease lost, or cannot
renew it before it lapses, cancels the task. An item whose lease expires (its process died, or
lost the database) is taken back and run again, by this process or another.

A Worker returns how the item ended: `Done` (or None), or `Later` to have it wait and run again.
One that raises fails the item, unless the database was lost under it: then the item is given
back to be tried again. A failed lease change (the database lost) is logged, and the item is
left to its lease, so it is taken back once that lapses.

The gate wakes when a submission or an item's end is notified (LISTEN on its own connection,
`db.listen`), when an item's `not_before` comes or a lease is due to lapse, and on a fallback
poll that covers a dropped listener: a drop delays work and loses none.

Lease changes run in the gate's own short units, outside any item's work context, so they are
never fenced, and they are attributed to the gate (`SYSTEM`)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg

from litwatch.core.work import Outcome, WorkItem
from litwatch.gates.context import Attribution, Work, attributed, working
from litwatch.gates.database import DatabaseGate, LeaseLostError, PoolTimeoutError, WriteUnit
from litwatch.store import work as queue

log = logging.getLogger(__name__)

# How long a lease lasts unrenewed. The heartbeat renews it every third of this, so a renewal
# kept waiting by an item's unit for seconds, or a database restart of a minute, loses
# nothing, while an item whose process died is taken back within this.
LEASE = timedelta(minutes=3)
# The fallback poll: the longest the gate sleeps without a notification.
POLL_SECONDS = 30.0
# The gate's own bookkeeping.
SYSTEM = Attribution.system("work gate")


@dataclass(frozen=True)
class Done:
    """The item did its work, spending `cost` dollars (None: unknown)."""

    cost: Decimal | None = None


@dataclass(frozen=True)
class Later:
    """The item cannot run yet (its pass is held by another owner, the run lock is taken): it
    waits until `at` and runs again."""

    at: datetime


type Result = Done | Later
# A Worker runs one item of its kind and says how it ended; None is `Done()`.
type Worker = Callable[[WorkItem], Awaitable[Result | None]]


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _lost(exc: BaseException) -> bool:
    """Whether `exc` says the database went away under the work (its connection dropped, the
    server shut down, the pool had none to give), not that a statement failed: such an item
    is tried again, not failed."""
    if isinstance(exc, PoolTimeoutError):
        return True
    if not isinstance(exc, psycopg.OperationalError):
        return False
    return exc.sqlstate is None or exc.sqlstate.startswith(("08", "57P"))


class WorkGate:
    """The work items of one database, as this process serves them. `lease` and `poll` are
    `LEASE` and `POLL_SECONDS` but in tests."""

    def __init__(
        self, db: DatabaseGate, *, lease: timedelta = LEASE, poll: float = POLL_SECONDS
    ) -> None:
        self.db, self.lease, self.poll = db, lease, poll
        self.listening = asyncio.Event()  # set while the listener is on
        self._wake = asyncio.Event()

    @asynccontextmanager
    async def _write(self, name: str) -> AsyncGenerator[WriteUnit]:
        """A unit of the gate's own bookkeeping."""
        with attributed(SYSTEM):
            async with self.db.write(f"work: {name}") as unit:
                yield unit

    async def take(self, kinds: list[str]) -> WorkItem | None:
        """Lease the next item of `kinds` that is due and admitted, if any."""
        try:
            async with self._write("take") as unit:
                return await queue.take(unit, kinds, self.lease)
        except psycopg.errors.UniqueViolation:
            # Another process took one for the same admission at once; its end wakes us.
            return None

    async def renew(self, item: WorkItem) -> bool:
        """Renew the lease on `item`; False when it is lost."""
        assert item.lease_token is not None  # a taken item has its lease
        async with self._write("renew") as unit:
            return await queue.renew(unit, item.id, item.lease_token, self.lease)

    async def take_back(self, kinds: list[str]) -> list[WorkItem]:
        """Take back the items of `kinds` whose lease expired."""
        async with self._write("take back") as unit:
            taken = await queue.take_back(unit, kinds)
        for item in taken:
            log.warning("work: took back %s %s (item %s), whose lease lapsed", *_name(item))
        return taken

    async def run(self, workers: Mapping[str, Worker]) -> None:
        """Serve the kinds of `workers`, each by its Worker, until cancelled. Cancelled, it
        cancels the items running and gives each back to be taken again at once."""
        kinds = sorted(workers)
        running: set[asyncio.Task[None]] = set()
        async with self._serving(kinds):
            log.info("work gate on: %s", ", ".join(kinds))
            try:
                while True:
                    self._wake.clear()
                    try:
                        await self.take_back(kinds)
                        while (item := await self.take(kinds)) is not None:
                            task = asyncio.create_task(self._work(item, workers[item.kind]))
                            running.add(task)
                            task.add_done_callback(running.discard)
                            task.add_done_callback(lambda _: self._wake.set())
                        async with self.db.read("work: next wait") as unit:
                            wait = await queue.wait_seconds(unit, kinds)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("work: taking items failed")
                        wait = None
                    timeout = self.poll if wait is None else min(self.poll, max(wait, 0.0))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout)
            finally:
                for task in running:
                    task.cancel()
                await asyncio.gather(*running, return_exceptions=True)

    @asynccontextmanager
    async def _serving(self, kinds: list[str]) -> AsyncGenerator[None]:
        """This process's `workers` row, kept beating, and the listener, for the block."""
        async with self._write("register") as unit:
            me = await queue.register(unit, f"{socket.gethostname()}:{os.getpid()}", kinds)
        tasks = [asyncio.create_task(self._listen()), asyncio.create_task(self._beat(me))]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                async with self._write("retire") as unit:
                    await queue.retire(unit, me)
            except Exception:
                log.warning("work: this process's worker row stays", exc_info=True)

    async def _beat(self, me: int) -> None:
        while True:
            await asyncio.sleep(self.lease.total_seconds() / 3)
            try:
                async with self._write("beat") as unit:
                    await queue.beat(unit, me)
            except Exception:
                log.warning("work: this process's heartbeat failed", exc_info=True)

    async def _listen(self) -> None:
        """Wake the gate on each notification, listening again after a drop."""
        while True:
            try:
                async with self.db.listen(queue.CHANNEL) as notes:
                    self.listening.set()
                    self._wake.set()  # what was notified while it was off
                    async for _ in notes:
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("work: the listener dropped; polling until it is back", exc_info=True)
            finally:
                self.listening.clear()
            await asyncio.sleep(self.poll)

    async def _work(self, item: WorkItem, worker: Worker) -> None:
        """Run `item` by `worker` under its lease and record how it ended."""
        handler = asyncio.create_task(self._handle(item, worker))
        lost = asyncio.Event()
        beat = asyncio.create_task(self._heartbeat(item, handler, lost))
        kind, key, number = _name(item)
        try:
            try:
                result = await handler
            except asyncio.CancelledError:
                if not lost.is_set():
                    # The gate is stopping: give the item back, for whoever runs next.
                    await self._settle("give back", lambda unit: queue.again(unit, item))
                    raise
                log.warning("work: %s %s (item %s) stopped: its lease is lost", kind, key, number)
                return
            except LeaseLostError:
                log.warning("work: %s %s (item %s) lost its lease", kind, key, number)
                return
            except Exception as exc:
                if _lost(exc):
                    log.warning("work: %s %s (item %s) lost the database: %s", *_name(item), exc)
                    await self._settle("try again", lambda unit: queue.again(unit, item))
                    return
                log.exception("work: %s %s (item %s) failed", kind, key, number)
                error = _describe(exc)
                await self._settle(
                    "fail", lambda unit: queue.end(unit, item, Outcome.FAILED, error=error)
                )
                return
            match result:
                case Later(at):
                    await self._settle("wait", lambda unit: queue.again(unit, item, at))
                case _:
                    cost = None if result is None else result.cost
                    await self._settle(
                        "end", lambda unit: queue.end(unit, item, Outcome.DONE, cost=cost)
                    )
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    async def _handle(self, item: WorkItem, worker: Worker) -> Result | None:
        """The Worker, in the item's own task, under its work context and attribution."""
        with working(Work(item)), attributed(Attribution.work(item.id)):
            return await worker(item)

    async def _heartbeat(self, item: WorkItem, handler: asyncio.Task, lost: asyncio.Event) -> None:
        """Renew the item's lease every third of it. A renewal that finds it lost, or no
        renewal by the time it lapses, sets `lost` and cancels the handler."""
        clock = asyncio.get_running_loop().time
        every = self.lease.total_seconds() / 3
        lapses = clock() + self.lease.total_seconds()
        while True:
            await asyncio.sleep(every)
            asked = clock()
            try:
                held = await self.renew(item)
            except Exception as exc:
                held = clock() + every < lapses  # still held until the next try, if that holds
                log.warning("work: renewing item %s's lease failed: %s", item.id, _describe(exc))
            else:
                if held:
                    lapses = asked + self.lease.total_seconds()
            if not held:
                lost.set()
                handler.cancel()
                return

    async def _settle(self, what: str, change: Callable[[WriteUnit], Awaitable[bool]]) -> None:
        """Record how an item ended, or that it runs again, in a unit of the gate's own. A
        change that fails (the database lost) is logged, and the item waits out its lease."""
        try:
            async with self._write(what) as unit:
                if not await change(unit):
                    log.warning("work: the item was taken back before it could %s", what)
        except Exception as exc:
            log.warning("work: could not %s the item, taken back later: %s", what, _describe(exc))


def _name(item: WorkItem) -> tuple[str, str, int]:
    return item.kind, item.key, item.id
