"""The Work gate (docs/specs/architecture/a2-work-gate.md; docs/plans/a2-work-gate.md, P1): it
admits work items, leases them to this process's Workers, renews and takes back their leases,
and records how each ended.

    gate = WorkGate(db)
    await gate.run({"fulltext": fulltext_worker, "deliver": deliver_worker})   # until cancelled

Work is submitted elsewhere, by `store.work.submit` in the submitter's own unit. `run` takes
what is due, one item at a time per admission (a kind, or an integration for deliveries). Each
claim has one supervisor, from the claim's commit to the item's settling: it runs the Worker in
a task of its own under the work context (`gates.context`), where the item's lease fences the
task's write units (`gates.database`) and its gate calls are attributed to the item, and it
keeps the lease beating. A renewal is bounded by the lease it renews: one that finds the lease
lost, or does not confirm it before it lapses, cancels the Worker, and whatever the Worker
returns after that is ignored. An item whose lease expires (its process died, or lost the
database) is taken back and run again, by this process or another.

A Worker returns how the item ended: `Done` (or None), or `Later` to have it wait and run again.
One that raises fails the item, unless the database was lost under it: then the item is given
back to be tried again. Settling needs the lease still live. A failed settling (the database
lost) is logged, and the item is left to its lease, so it is taken back once that lapses.

Stopping (`run` cancelled) has one path: each supervisor cancels its Worker, waits for it to
stop, and gives back the claim it still owns, a claim whose commit it was waiting on included;
a settling under way finishes first.

The gate wakes when a submission or an item's settling is notified (LISTEN on its own
connection, `db.listen`), when an item's `not_before` comes or a lease is due to lapse, and on a
fallback poll that covers a dropped listener: a drop delays work and loses none.

Everything the gate does for itself (claims, leases, settling, the listener, its reads) is
attributed to the gate (`SYSTEM`) and runs outside any item's work context, so it is never
fenced."""

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
# How long the gate waits before looking again at work it found due but could not take (a row
# a unit holds, a claim a peer is making), and before registering again after the database
# was lost.
RETRY_SECONDS = 1.0
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

# The database errors that say the database went away, whatever class psycopg gives them: an
# idle-in-transaction and a transaction timeout end the session (`InternalError`).
_SESSION_ENDED = ("25P03", "25P04")


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _lost(exc: BaseException) -> bool:
    """Whether `exc` says the database went away under the work (its connection dropped, the
    server shut down or ended the session, the pool had none to give), not that a statement
    failed: such an item is tried again, not failed. A group of errors says so when each of
    them does."""
    if isinstance(exc, BaseExceptionGroup):
        return all(_lost(e) for e in exc.exceptions)
    if isinstance(exc, PoolTimeoutError):
        return True
    if isinstance(exc, psycopg.Error) and exc.sqlstate in _SESSION_ENDED:
        return True
    if not isinstance(exc, psycopg.OperationalError):
        return False
    return exc.sqlstate is None or exc.sqlstate.startswith(("08", "57P"))


async def _finished[T](task: asyncio.Task[T]) -> None:
    """Wait for `task` to finish, and for it alone: cancelled meanwhile, still wait, then raise."""
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait({task})
        raise
    except Exception:
        pass  # the task's own error is its owner's to read


class WorkGate:
    """The work items of one database, as this process serves them. `lease` and `poll` are
    `LEASE` and `POLL_SECONDS` but in tests."""

    def __init__(
        self, db: DatabaseGate, *, lease: timedelta = LEASE, poll: float = POLL_SECONDS
    ) -> None:
        self.db, self.lease, self.poll = db, lease, poll
        self.listening = asyncio.Event()  # set while the listener is on
        self._wake = asyncio.Event()
        self._waiting = False
        # Renewals cut off at their lease's end, kept until they have unwound.
        self._unwinding: set[asyncio.Task[bool]] = set()

    @property
    def idle(self) -> bool:
        """Whether `run` sleeps with nothing to wake it yet."""
        return self._waiting and not self._wake.is_set()

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
            # Another process took one for the same admission at once; its settling wakes us.
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
        """Serve the kinds of `workers`, each by its Worker, until cancelled. Cancelled, each
        claim's supervisor stops its Worker and gives back the claim it still owns."""
        kinds = sorted(workers)
        supervisors: set[asyncio.Task[None]] = set()
        with attributed(SYSTEM):
            async with self._serving(kinds):
                log.info("work gate on: %s", ", ".join(kinds))
                try:
                    while True:
                        self._wake.clear()
                        wait: float | None = None
                        try:
                            await self.take_back(kinds)
                            while await self._claim(kinds, workers, supervisors):
                                pass
                            async with self.db.read("work: next wait") as unit:
                                wait = await queue.wait_seconds(unit, kinds)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.exception("work: taking items failed")
                        # Due but not taken: a row a unit holds, or a peer's claim. Look again
                        # shortly rather than at once.
                        if wait is not None:
                            wait = max(wait, RETRY_SECONDS)
                        self._waiting = True
                        try:
                            with contextlib.suppress(TimeoutError):
                                await asyncio.wait_for(
                                    self._wake.wait(), min(self.poll, wait or self.poll)
                                )
                        finally:
                            self._waiting = False
                finally:
                    for task in supervisors:
                        task.cancel()
                    await asyncio.gather(*supervisors, return_exceptions=True)

    async def _claim(
        self, kinds: list[str], workers: Mapping[str, Worker], supervisors: set[asyncio.Task[None]]
    ) -> bool:
        """Start a supervisor on the next claim; whether there was one to take."""
        claimed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._supervise(kinds, workers, claimed))
        supervisors.add(task)
        task.add_done_callback(supervisors.discard)
        if not await asyncio.shield(claimed):
            return False
        task.add_done_callback(lambda _: self._wake.set())  # its admission is free again
        return True

    async def _supervise(
        self, kinds: list[str], workers: Mapping[str, Worker], claimed: asyncio.Future[bool]
    ) -> None:
        """Own one claim from its commit to its settling: take it, run its Worker, settle it."""
        since = asyncio.get_running_loop().time()  # no later than the lease's start
        taking = asyncio.create_task(self.take(kinds))
        try:
            await _finished(taking)
        except asyncio.CancelledError:
            # The claim may have committed while we were stopped: it is ours to give back.
            if not taking.cancelled() and taking.exception() is None and (item := taking.result()):
                await self._give_back(item)
            if not claimed.done():
                claimed.cancel()
            raise
        if (error := taking.exception()) is not None:
            claimed.set_exception(error)
            return
        item = taking.result()
        claimed.set_result(item is not None)
        if item is not None:
            await self._run(item, workers[item.kind], since)

    async def _run(self, item: WorkItem, worker: Worker, since: float) -> None:
        """Run `item` by `worker` under its lease and settle it."""
        lost = asyncio.Event()
        handler = asyncio.create_task(self._handle(item, worker))
        beat = asyncio.create_task(self._heartbeat(item, handler, lost, since))
        try:
            try:
                await asyncio.wait({handler})
            except asyncio.CancelledError:
                handler.cancel()
                await asyncio.wait({handler})
                if not lost.is_set():
                    await self._give_back(item)
                raise
            if lost.is_set():
                log.warning("work: %s %s (item %s) stopped: its lease is lost", *_name(item))
                return
            if handler.cancelled():  # by no one here: it stopped, so it is given back
                await self._give_back(item)
                return
            await self._settle(item, handler)
        finally:
            beat.cancel()
            await asyncio.wait({beat})

    async def _handle(self, item: WorkItem, worker: Worker) -> Result | None:
        """The Worker, in the item's own task, under its work context and attribution."""
        with working(Work(item)), attributed(Attribution.work(item.id)):
            return await worker(item)

    async def _settle(self, item: WorkItem, handler: asyncio.Task[Result | None]) -> None:
        """Record how the Worker's run ended."""
        error = handler.exception()
        if isinstance(error, LeaseLostError):
            log.warning("work: %s %s (item %s) lost its lease", *_name(item))
        elif error is not None and _lost(error):
            log.warning("work: %s %s (item %s) lost the database: %s", *_name(item), error)
            await self._change("try again", lambda unit: queue.again(unit, item))
        elif error is not None:
            log.error("work: %s %s (item %s) failed", *_name(item), exc_info=error)
            failure = _describe(error)
            await self._change(
                "fail", lambda unit: queue.end(unit, item, Outcome.FAILED, error=failure)
            )
        elif isinstance(result := handler.result(), Later):
            at = result.at
            await self._change("wait", lambda unit: queue.again(unit, item, at))
        else:
            cost = None if result is None else result.cost
            await self._change("end", lambda unit: queue.end(unit, item, Outcome.DONE, cost=cost))

    async def _give_back(self, item: WorkItem) -> None:
        """Give back a claim whose Worker has stopped (or never started), its lease live or not,
        so whoever runs next takes it at once."""
        await self._change("give back", lambda unit: queue.again(unit, item, live=False))

    async def _change(self, what: str, change: Callable[[WriteUnit], Awaitable[bool]]) -> None:
        """Make a change to a claim in a unit of the gate's own, carried through if the gate
        stops meanwhile. One that fails (the database lost) is logged, and the item waits out
        its lease."""

        async def changing() -> None:
            try:
                async with self._write(what) as unit:
                    if not await change(unit):
                        log.warning("work: could not %s the item: its lease is gone", what)
            except Exception as exc:
                log.warning("work: could not %s the item, taken back later: %s", what, exc)

        await _finished(asyncio.create_task(changing()))

    async def _heartbeat(
        self, item: WorkItem, handler: asyncio.Task, lost: asyncio.Event, since: float
    ) -> None:
        """Renew the item's lease every third of it. Each renewal, from asking the pool for a
        connection to its commit, must confirm the lease before the lease last confirmed lapses
        (taken from `since`, a time no later than the lease's start). When a renewal finds the
        lease lost, or the lease lapses first, set `lost` and cancel the handler at once."""
        clock = asyncio.get_running_loop().time
        lease = self.lease.total_seconds()
        lapses, next_try = since + lease, since + lease / 3
        while True:
            await asyncio.sleep(max(0.0, min(next_try, lapses) - clock()))
            asked = clock()
            if asked >= lapses:
                break
            renewal = asyncio.create_task(self.renew(item))
            try:
                done, _ = await asyncio.wait({renewal}, timeout=lapses - asked)
            except asyncio.CancelledError:
                renewal.cancel()
                raise
            if not done:
                renewal.cancel()  # left to unwind on its own: the handler stops first
                self._unwinding.add(renewal)
                renewal.add_done_callback(self._unwinding.discard)
                break
            if (error := renewal.exception()) is not None:
                log.warning("work: renewing item %s's lease failed: %s", item.id, error)
                next_try = clock() + lease / 3
                continue
            if not renewal.result():
                break
            lapses, next_try = asked + lease, asked + lease / 3
        lost.set()
        handler.cancel()

    @asynccontextmanager
    async def _serving(self, kinds: list[str]) -> AsyncGenerator[None]:
        """This process's `workers` row, kept beating, and the listener, for the block."""
        me = await self._register(kinds)
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

    async def _register(self, kinds: list[str]) -> int:
        """Record this process as serving `kinds`, trying again while the database is lost."""
        while True:
            try:
                async with self._write("register") as unit:
                    name = f"{socket.gethostname()}:{os.getpid()}"
                    return await queue.register(unit, name, kinds)
            except Exception as exc:
                if not _lost(exc):
                    raise
                log.warning("work: registering failed, trying again: %s", exc)
            await asyncio.sleep(RETRY_SECONDS)

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
                    self._wake.set()  # what was notified while it was off
                    self.listening.set()
                    async for _ in notes:
                        self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("work: the listener dropped; polling until it is back", exc_info=True)
            finally:
                self.listening.clear()
            await asyncio.sleep(self.poll)


def _name(item: WorkItem) -> tuple[str, str, int]:
    return item.kind, item.key, item.id
