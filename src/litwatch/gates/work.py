"""The Work gate (docs/specs/architecture/a2-work-gate.md; docs/plans/a2-work-gate.md, P1): it
admits work items, leases them to this process's Workers, renews and takes back their leases,
and records how each ended.

    gate = WorkGate(db)
    await gate.run({"fulltext": fulltext_worker, "deliver": deliver_worker})   # until cancelled

Work is submitted elsewhere, by `store.work.submit` in the submitter's own unit. `run` takes
what is due, one item at a time per admission (a kind, a slot of text passes, or an integration
for deliveries). The run loop takes each claim through its commit and hands it to a supervisor
of its own, which owns it to the item's settling: it runs the Worker in a task of its own under
the work context (`gates.context`), where the item's lease fences the task's write units
(`gates.database`) and its gate calls are attributed to the item, and it keeps the lease
beating. A lease starts when its claim wins admission, and a claim whose lease ran out before
its Worker could start is given back. A renewal is bounded by the lease it renews: one that
fails, finds the lease lost, or does not confirm it before it lapses, cancels the Worker, and
whatever the Worker returns after that is ignored. An item whose lease expires (its process
died, or lost the database) is taken back and run again, by this process or another.

A Worker returns how the item ended: `Done` (or None), or `Later` to have it wait and run again.
One that raises fails the item, unless the database was lost under it, or a statement lost a
deadlock or a serialization conflict to another transaction (`conflicted`): then the item is
given back to be tried again, so a turn or a pass is never left with no item to run it.
Settling needs the lease still live. A failed settling (the database lost) is logged, and the
item is left to its lease, so it is taken back once that lapses.

Stopping (`run` cancelled) has one path: each supervisor cancels its Worker, waits for it to
stop, and gives back the claim it still owns, and a claim whose commit the run loop was waiting
on is given back too; a settling under way finishes first.

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
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

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
    """The item did its work, spending `cost` dollars (None: unknown); `result`, a CLI
    command's outcome, is kept with the item, in the unit that ends it."""

    cost: Decimal | None = None
    result: Any = None


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


def database_lost(exc: BaseException) -> bool:
    """Whether `exc` says the database went away under the work (its connection dropped, the
    server shut down or ended the session, the pool had none to give), not that a statement
    failed: such an item is tried again, not failed. A group of errors says so when each of
    them does."""
    if isinstance(exc, BaseExceptionGroup):
        return all(database_lost(e) for e in exc.exceptions)
    if isinstance(exc, PoolTimeoutError):
        return True
    if isinstance(exc, psycopg.Error) and exc.sqlstate in _SESSION_ENDED:
        return True
    if not isinstance(exc, psycopg.OperationalError):
        return False
    return exc.sqlstate is None or exc.sqlstate.startswith(("08", "57P"))


# The errors of a statement another transaction made fail, which succeeds when tried again: a
# deadlock it lost, a serialization conflict.
_CONFLICTS = ("40P01", "40001")


def conflicted(exc: BaseException) -> bool:
    """Whether `exc` says a statement lost a deadlock or a serialization conflict to another
    transaction: such an item is tried again, not failed. A group of errors says so when each
    of them does."""
    if isinstance(exc, BaseExceptionGroup):
        return all(conflicted(e) for e in exc.exceptions)
    return isinstance(exc, psycopg.Error) and exc.sqlstate in _CONFLICTS


def retryable(exc: BaseException) -> bool:
    """Whether the item `exc` ended is tried again rather than failed: the database was lost
    under it, or a statement lost a conflict (`database_lost`, `conflicted`); a group of errors
    when each of them is one or the other (a task group's, the digests')."""
    if isinstance(exc, BaseExceptionGroup):
        return all(retryable(e) for e in exc.exceptions)
    return database_lost(exc) or conflicted(exc)


async def _finished[T](task: asyncio.Task[T]) -> None:
    """Wait for `task` to finish, and for it alone: cancelled meanwhile, still wait, then raise."""
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait({task})
        raise
    except Exception:
        pass  # the task's own error is its owner's to read


def _stop(handler: asyncio.Task) -> None:
    """Cancel `handler` unless it was cancelled already: a second cancel would cut its cleanup
    (its run's close, its claims' release) short."""
    if not handler.cancelling():
        handler.cancel()


async def _stop_when(cancelling: asyncio.Event, handler: asyncio.Task) -> None:
    """Cancel `handler` once its owner asked to cancel its item."""
    await cancelling.wait()
    _stop(handler)


def _cancelled(unit: WriteUnit, item: WorkItem) -> Awaitable[bool]:
    """End the item its owner cancelled, once its Worker has stopped, its lease live or not."""
    return queue.end(unit, item, Outcome.FAILED, error=queue.CANCELLED, live=False)


def _reap(item: WorkItem, handler: asyncio.Task) -> None:
    """Log the error a Worker stopped by the gate raised as it stopped, if it raised one."""
    if not handler.cancelled() and (error := handler.exception()) is not None:
        log.warning("work: %s %s (item %s) raised as it stopped", *_name(item), exc_info=error)


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
        self._keys: Mapping[str, Collection[str]] | None = None  # as `run` was given them
        # The items running here, each with what is set when its owner asks to cancel it.
        self._running: dict[int, asyncio.Event] = {}
        self._retire: Callable[[], Awaitable[None]] | None = None  # while `run` serves

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

    async def _take(self, kinds: list[str]) -> tuple[WorkItem, float] | None:
        """Lease the next item of `kinds` that is due and admitted, if any, with a time on the
        event loop's clock no later than its lease's start: the lease runs from it."""
        try:
            async with self._write("take") as unit:
                claimed = await queue.take(unit, kinds, self.lease, self._keys)
                if claimed is None:
                    return None
                since = asyncio.get_running_loop().time()
                return await queue.stamp(unit, claimed, self.lease), since
        except psycopg.errors.UniqueViolation:
            # Another process took one for the same admission at once; its settling wakes us.
            return None

    async def _unwound(self) -> None:
        """Wait for the renewals cut off at their lease's end (`_heartbeat`) to unwind."""
        await asyncio.gather(*self._unwinding, return_exceptions=True)

    async def renew(self, item: WorkItem) -> bool:
        """Renew the lease on `item`; False when it is lost."""
        assert item.lease_token is not None  # a taken item has its lease
        async with self._write("renew") as unit:
            return await queue.renew(unit, item.id, item.lease_token, self.lease)

    async def take_back(self, kinds: list[str]) -> list[WorkItem]:
        """Take back the items of `kinds` whose lease expired."""
        async with self._write("take back") as unit:
            taken = await queue.take_back(unit, kinds, self._keys)
        for item in taken:
            log.warning("work: took back %s %s (item %s), whose lease lapsed", *_name(item))
        return taken

    async def run(
        self,
        workers: Mapping[str, Worker],
        *,
        keys: Mapping[str, Collection[str]] | None = None,
        supersede: bool = False,
    ) -> None:
        """Serve the kinds of `workers`, each by its Worker, until cancelled; of a kind in
        `keys`, only the items of those keys (an integration's deliveries, which only a process
        that loads it can run). Cancelled, each claim's supervisor stops its Worker and gives
        back the claim it still owns; a command's, tried once, ends failed instead
        (`store.work.once`). With `supersede` (`serve`'s gate), this process's `workers` row
        replaces those of the processes before it on this host."""
        kinds = sorted(workers)
        self._keys = keys
        supervisors: set[asyncio.Task[None]] = set()
        with attributed(SYSTEM):
            async with self._serving(kinds, supersede=supersede):
                log.info("work gate on: %s", ", ".join(kinds))
                try:
                    while True:
                        self._wake.clear()
                        wait: float | None = None
                        try:
                            await self._cancels()
                            await self.take_back(kinds)
                            while await self._claim(kinds, workers, supervisors):
                                pass
                            async with self.db.read("work: next wait") as unit:
                                wait = await queue.wait_seconds(unit, kinds, self._keys)
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
                    # No more taken from now: no command is accepted for a kind no one runs.
                    if self._retire is not None:
                        await self._retire()
                    for task in supervisors:
                        task.cancel()
                    await asyncio.gather(*supervisors, return_exceptions=True)
                    # Renewals cut off are cancelled already; their Workers have stopped.
                    await self._unwound()

    async def _claim(
        self, kinds: list[str], workers: Mapping[str, Worker], supervisors: set[asyncio.Task[None]]
    ) -> bool:
        """Take the next claim, through its commit, and hand it to a supervisor of its own;
        whether there was one to take. Stopped meanwhile, a claim that committed is given
        back."""
        taking = asyncio.create_task(self._take(kinds))
        try:
            await _finished(taking)
        except asyncio.CancelledError:
            if not taking.cancelled() and taking.exception() is None and (taken := taking.result()):
                await self._give_back(taken[0])
            raise
        if (taken := taking.result()) is None:
            return False
        item, since = taken
        task = asyncio.create_task(self._supervise(item, workers[item.kind], since))
        supervisors.add(task)
        task.add_done_callback(supervisors.discard)
        task.add_done_callback(lambda _: self._wake.set())  # its admission is free again
        return True

    async def _cancels(self) -> None:
        """Tell the items running here whose owner asked to cancel them (`store.work.cancel`)."""
        if not self._running:
            return
        async with self.db.read("work: cancels asked") as unit:
            asked = await queue.asked_to_cancel(unit, list(self._running))
        for item_id in asked:
            if (cancelling := self._running.get(item_id)) is not None:
                cancelling.set()

    async def _supervise(self, item: WorkItem, worker: Worker, since: float) -> None:
        """Own one committed claim to its settling: run its Worker under its lease, unless the
        lease ran out before it could start, or its owner cancelled it before."""
        if item.cancel_asked is not None:
            await self._change("cancel", lambda unit: _cancelled(unit, item))
            return
        if asyncio.get_running_loop().time() >= since + self.lease.total_seconds():
            # Its lease ran out before it could start: whoever runs next takes it.
            log.warning("work: %s %s (item %s) leased too late to start", *_name(item))
            await self._give_back(item)
            return
        await self._run(item, worker, since)

    async def _run(self, item: WorkItem, worker: Worker, since: float) -> None:
        """Run `item` by `worker` under its lease and settle it. The Worker is cancelled once at
        most, so its cleanup runs to its end under the claim: by the gate stopping, its lease
        lost, or (a command's) its owner's cancel, after which the item ends failed."""
        lost, cancelling = asyncio.Event(), asyncio.Event()
        handler = asyncio.create_task(self._handle(item, worker))
        beat = asyncio.create_task(self._heartbeat(item, handler, lost, since))
        stopper = None
        if queue.once(item.kind):  # only a command's owner cancels it
            stopper = asyncio.create_task(_stop_when(cancelling, handler))
            self._running[item.id] = cancelling
        try:
            try:
                await asyncio.wait({handler})
            except asyncio.CancelledError:
                _stop(handler)  # unless the heartbeat or the cancel did: cleanup runs once
                await asyncio.wait({handler})
                _reap(item, handler)
                if cancelling.is_set():
                    await self._change("cancel", lambda unit: _cancelled(unit, item))
                elif not lost.is_set():
                    await self._give_back(item)
                raise
            if lost.is_set():
                log.warning("work: %s %s (item %s) stopped: its lease is lost", *_name(item))
                _reap(item, handler)
                return
            if cancelling.is_set() and (handler.cancelled() or handler.exception() is not None):
                log.warning("work: %s %s (item %s) cancelled by its owner", *_name(item))
                _reap(item, handler)
                await self._change("cancel", lambda unit: _cancelled(unit, item))
                return
            if handler.cancelled():  # by no one here: it stopped, so it is given back
                await self._give_back(item)
                return
            await self._settle(item, handler)
        finally:
            self._running.pop(item.id, None)
            if stopper is not None:
                stopper.cancel()
            beat.cancel()
            try:
                await asyncio.wait({beat})
            except asyncio.CancelledError:  # its renewal still ends before `run` returns
                await asyncio.wait({beat})
                raise

    async def _handle(self, item: WorkItem, worker: Worker) -> Result | None:
        """The Worker, in the item's own task, under its work context and attribution."""
        with working(Work(item)), attributed(Attribution.work(item.id)):
            return await worker(item)

    async def _settle(self, item: WorkItem, handler: asyncio.Task[Result | None]) -> None:
        """Record how the Worker's run ended."""
        error = handler.exception()
        if isinstance(error, LeaseLostError):
            log.warning("work: %s %s (item %s) lost its lease", *_name(item))
        elif error is not None and retryable(error) and queue.once(item.kind):
            log.warning("work: %s %s (item %s) stopped: %s", *_name(item), error)
            lost_db = f"{queue.STOPPED}: {_describe(error)}"
            await self._change(
                "fail", lambda unit: queue.end(unit, item, Outcome.FAILED, error=lost_db)
            )
        elif error is not None and retryable(error):
            log.warning("work: %s %s (item %s) is tried again: %s", *_name(item), error)
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
            cost, kept = (None, None) if result is None else (result.cost, result.result)
            await self._change(
                "end", lambda unit: queue.end(unit, item, Outcome.DONE, cost=cost, result=kept)
            )

    async def _give_back(self, item: WorkItem) -> None:
        """Give back a claim whose Worker has stopped (or never started), its lease live or not,
        so whoever runs next takes it at once; a command's ends failed instead (`STOPPED`), and
        its command submits it again."""
        if queue.once(item.kind):
            await self._change(
                "end",
                lambda unit: queue.end(unit, item, Outcome.FAILED, error=queue.STOPPED, live=False),
            )
            return
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
        (counted from `since`, a time no later than the lease's start). When a renewal fails,
        finds the lease lost, or the lease lapses first, set `lost` and cancel the handler at
        once."""
        clock = asyncio.get_running_loop().time
        lease = self.lease.total_seconds()
        while True:
            lapses = since + lease
            await asyncio.sleep(max(0.0, since + lease / 3 - clock()))
            asked = clock()
            renewal = asyncio.create_task(self.renew(item))
            try:
                done, _ = await asyncio.wait({renewal}, timeout=lapses - asked)
            except asyncio.CancelledError:
                renewal.cancel()  # its unit ends before the gate's `run` returns
                await asyncio.wait({renewal})
                raise
            if not done:
                renewal.cancel()  # left to unwind on its own: the handler stops first
                self._unwinding.add(renewal)
                renewal.add_done_callback(self._unwinding.discard)
                break
            if (error := renewal.exception()) is not None:
                log.warning("work: renewing item %s's lease failed: %s", item.id, error)
                break
            if not renewal.result():
                break
            since = asked
        lost.set()
        _stop(handler)

    @asynccontextmanager
    async def _serving(self, kinds: list[str], *, supersede: bool) -> AsyncGenerator[None]:
        """This process's `workers` row, kept beating, and the listener, for the block. The row
        goes when the block ends, or sooner, as the gate stops taking work (`_retire`), so no
        command is accepted for a kind no one will run; a process killed leaves it until its
        heartbeat is a lease old."""
        me = await self._register(kinds, supersede=supersede)
        tasks = [asyncio.create_task(self._listen()), asyncio.create_task(self._beat(me))]
        retired = False

        async def retire() -> None:
            nonlocal retired
            if retired:
                return
            retired = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                async with self._write("retire") as unit:
                    await queue.retire(unit, me)
            except Exception:
                log.warning("work: this process's worker row stays", exc_info=True)

        self._retire = retire
        try:
            yield
        finally:
            self._retire = None
            await retire()

    async def _register(self, kinds: list[str], *, supersede: bool) -> int:
        """Record this process as serving `kinds` (superseding its host's earlier processes,
        with `supersede`), trying again while the database is lost."""
        host = socket.gethostname()
        while True:
            try:
                async with self._write("register") as unit:
                    return await queue.register(
                        unit, f"{host}:{os.getpid()}", kinds, supersedes=host if supersede else None
                    )
            except Exception as exc:
                if not database_lost(exc):
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
