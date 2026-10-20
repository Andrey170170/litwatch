"""Message Batches inside the gateway (docs/specs/v1/models.md, batches). A batching gateway hands
each call it cannot reuse to its provider's `Batcher`, which holds the calls until none has joined
for a while, sends them as one batch, polls it, and answers each held call with its reply, or with
the account error the provider gave the request instead, which is never asked again. A call
it cannot answer from a batch (the deadline passed, the batch did not answer it, the provider
failed) gets None, and the gateway sends it as a normal call. A batch given up on while still open
(the drain ran out, the gateway closed) may still answer: the journal rows its requests are on
are `unsettled`, left for a later run to account for, and their normal calls get rows of their
own."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from litwatch.config import RoleConfig
from litwatch.llm.types import ProviderError, Reply, Request, Usage

log = logging.getLogger(__name__)

QUIET_SECONDS = 10.0  # a batch goes out once no call has joined it for this long
MAX_REQUESTS = 10_000  # or once it holds this many (Anthropic allows 100,000)
MAX_BYTES = 200_000_000  # or this much (Anthropic allows 256 MB)
POLL_SECONDS = 60.0
OPERATION_SECONDS = 600.0  # a submission, status, cancel or results read, as a whole
DRAIN = timedelta(minutes=30)  # how long cancelled batches are waited for after the deadline

# What a batch gives a call: its reply, the account error it got instead, or None (not answered).
type Answered = Reply | ProviderError | None


@runtime_checkable
class BatchProvider(Protocol):
    """A provider that batches (anthropic.AnthropicProvider). `custom_id` is the request key."""

    name: str

    async def submit(self, requests: list[tuple[str, RoleConfig, Request]]) -> str: ...
    async def status(self, batch_id: str) -> str: ...  # in_progress, canceling, ended or gone
    async def cancel(self, batch_id: str) -> None: ...
    async def results(self, batch_id: str) -> dict[str, Answered]: ...


class BatchJournal(Protocol):
    async def batched(self, call_ids: list[int], batch_id: str, provider: str) -> None: ...
    async def answered(
        self, batch_id: str, key: str, usage: Usage | None, error: str | None = None
    ) -> None: ...
    async def gone(self, batch_id: str) -> None: ...
    async def account_error(self, provider: str, error: str) -> None: ...


def _size(request: Request) -> int:
    """About what a request adds to a batch: its texts and schema as JSON, in UTF-8 bytes, and
    the envelope."""
    parts = [request.system, request.user, request.output.model_json_schema()]
    return len(json.dumps(parts, ensure_ascii=False).encode()) + 1_000


@dataclass
class _Held:
    key: str
    role: RoleConfig
    request: Request
    call_id: int | None
    size: int


@dataclass
class _Open:
    """A batch sent: the futures awaiting its answers, and the journal rows its requests are
    on, by request key."""

    waiting: dict[str, asyncio.Future[Answered]] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)
    cancelled: bool = False


class Batcher:
    """One provider's batches for one job, until `deadline` (UTC)."""

    def __init__(
        self,
        provider: BatchProvider,
        journal: BatchJournal | None,
        deadline: datetime,
        *,
        quiet: float = QUIET_SECONDS,
        poll: float = POLL_SECONDS,
        max_requests: int = MAX_REQUESTS,
        max_bytes: int = MAX_BYTES,
        drain: timedelta = DRAIN,
        operation: float = OPERATION_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.provider, self.journal, self.deadline = provider, journal, deadline
        self.quiet, self.poll, self.drain, self.now = quiet, poll, drain, now
        self.max_requests, self.max_bytes, self.operation = max_requests, max_bytes, operation
        self._held: list[_Held] = []
        self._futures: dict[str, asyncio.Future[Answered]] = {}  # held or sent, by key
        self._open: dict[str, _Open] = {}
        self._joined = 0.0  # when the last call joined the held ones (monotonic)
        self.unsettled: set[int] = set()  # journal rows of requests given up on, batch open
        self._flusher: asyncio.Task[None] | None = None
        self._poller: asyncio.Task[None] | None = None

    def open(self) -> bool:
        return self.now() < self.deadline

    async def ask(
        self, key: str, role: RoleConfig, request: Request, call_id: int | None
    ) -> Answered:
        """The reply from a batch, its account error, or None: send it as a normal call. An
        identical call held or sent already shares its request, and its reply at no cost."""
        if not self.open():
            return None
        if key in self._futures:
            return await self._shared(self._futures[key])
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        size = _size(request)
        self._held.append(_Held(key, role, request, call_id, size))
        self._joined = time.monotonic()
        if self._flusher is None or self._flusher.done():
            self._flusher = self._supervised(self._flush_when_quiet())
        return await asyncio.shield(future)

    async def attach(self, batch_id: str, key: str, call_id: int) -> Answered:
        """The reply of a call a dead run sent in batch `batch_id`, on journal row `call_id`."""
        if key in self._futures:
            return await self._shared(self._futures[key])
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        batch = self._open.setdefault(batch_id, _Open())
        batch.waiting[key], batch.rows[key] = future, call_id
        self._start_polling()
        return await asyncio.shield(future)

    @staticmethod
    async def _shared(future: asyncio.Future[Answered]) -> Answered:
        reply = await asyncio.shield(future)
        if not isinstance(reply, Reply):
            return reply
        usage = reply.usage.model_copy(
            update={"cost": 0.0, "reused": True, "original_cost": reply.usage.cost}
        )
        return dataclasses.replace(reply, usage=usage)

    def watch(self, batch_id: str) -> None:
        """Read batch `batch_id` once it ends, so the charges of its answers no call awaits are
        journalled (a dead run's batch)."""
        self._open.setdefault(batch_id, _Open())
        self._start_polling()

    async def settle(self) -> None:
        """Wait until every open batch is read, or given up on after the deadline's drain."""
        if self._poller is not None:
            await asyncio.shield(self._poller)

    async def close(self) -> None:
        """Stop collecting and polling; whatever still waits is sent as normal calls."""
        for task in (self._flusher, self._poller):
            if task is not None and not task.done():  # one that failed has failed its calls
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._held = []
        for batch in self._open.values():
            self._give_up(batch)
        self._open = {}
        for key in list(self._futures):
            self._resolve(key, None)

    def _supervised(self, work: Coroutine[None, None, None]) -> asyncio.Task[None]:
        """A background task whose failure (the journal's, say) fails every call still
        waiting, rather than leaving them waiting for good."""

        def failed(task: asyncio.Task[None]) -> None:
            if task.cancelled() or (exc := task.exception()) is None:
                return
            log.error("batches stopped: %r", exc)
            self._held, self._open = [], {}
            for future in self._futures.values():
                if not future.done():
                    future.set_exception(exc)
            self._futures = {}

        task = asyncio.create_task(work)
        task.add_done_callback(failed)
        return task

    # -- collecting ----------------------------------------------------------------------------

    def _full(self) -> bool:
        return (
            len(self._held) >= self.max_requests
            or sum(h.size for h in self._held) >= self.max_bytes
        )

    async def _flush_when_quiet(self) -> None:
        while self._held:
            left = self._joined + self.quiet - time.monotonic()
            if left > 0 and not self._full() and self.open():
                await asyncio.sleep(min(left, 1.0))
                continue
            count, size = 0, 0  # a batch within both ceilings, of one call at least
            for h in self._held[: self.max_requests]:
                if count and size + h.size > self.max_bytes:
                    break
                count, size = count + 1, size + h.size
            take, self._held = self._held[:count], self._held[count:]
            await self._send(take)

    async def _send(self, held: list[_Held]) -> None:
        if not self.open():
            for h in held:
                self._resolve(h.key, None)
            return
        try:
            batch_id = await self._bounded(
                self.provider.submit([(h.key, h.role, h.request) for h in held])
            )
        except Exception as exc:
            account = isinstance(exc, ProviderError) and exc.account
            if account:  # never asked again: the calls fail on the account
                log.error("a batch of %d calls was refused for the account: %s", len(held), exc)
            else:
                log.exception(
                    "a batch of %d calls could not be sent; they go as normal calls", len(held)
                )
            for h in held:
                self._resolve(h.key, exc if account else None)
            return
        log.info("batch %s sent with %d calls", batch_id, len(held))
        if self.journal:
            ids = [h.call_id for h in held if h.call_id is not None]
            await self.journal.batched(ids, batch_id, held[0].role.provider)
        self._open[batch_id] = _Open(
            {h.key: self._futures[h.key] for h in held},
            {h.key: h.call_id for h in held if h.call_id is not None},
        )
        self._start_polling()

    # -- polling -------------------------------------------------------------------------------

    def _start_polling(self) -> None:
        if self._poller is None or self._poller.done():
            self._poller = self._supervised(self._poll())

    async def _poll(self) -> None:
        while self._open:
            now = self.now()
            looked = list(self._open.items())
            # Together, so a pass takes two operations at most however many batches are open;
            # a failure is raised once every check has ended, none left running past it.
            checks = (self._check(i, b, now) for i, b in looked)
            ended = await asyncio.gather(*checks, return_exceptions=True)
            if failed := next((e for e in ended if isinstance(e, BaseException)), None):
                raise failed
            if self.now() >= self.deadline + self.drain:  # each given up looked at first
                # A batch read meanwhile is gone, or open again for a call attached since.
                left = [(i, b) for i, b in looked if self._open.get(i) is b]
                if left:
                    log.warning("%d batches still open after the deadline's drain", len(left))
                for batch_id, batch in left:
                    del self._open[batch_id]
                    self._give_up(batch)
                    for key in batch.waiting:
                        self._resolve(key, None)
                continue  # batches attached meanwhile, if any, are looked at now
            if self._open:
                await asyncio.sleep(self.poll)

    async def _check(self, batch_id: str, batch: _Open, now: datetime) -> None:
        replies: dict[str, Answered] = {}
        try:
            status = await self._bounded(self.provider.status(batch_id))
            if status not in ("ended", "gone"):
                if now >= self.deadline and not batch.cancelled:
                    await self._bounded(self.provider.cancel(batch_id))
                    batch.cancelled = True
                    log.info("batch %s cancelled at the deadline", batch_id)
                return
            if status == "ended":
                replies = await self._bounded(self.provider.results(batch_id))
        except Exception as exc:
            if not (isinstance(exc, ProviderError) and exc.account):
                log.exception("batch %s could not be read; trying again", batch_id)
                return
            # Not read again this run: its calls fail on the account, and the batch is left
            # open on its rows for a later run to account for. Seen even when no call waits on
            # it (a dead run's batch): the provider is failed for its account.
            log.error("batch %s could not be read for the account: %s", batch_id, exc)
            if self.journal:
                await self.journal.account_error(self.provider.name, f"account: {exc}")
            del self._open[batch_id]
            self._give_up(batch)
            for key in batch.waiting:
                self._resolve(key, exc)
            return
        if status == "gone":  # deleted, or another workspace's: it never answers
            log.warning("batch %s is gone from its provider", batch_id)
        if self.journal:  # the batch is accounted for before it is let go
            if status == "gone":
                await self.journal.gone(batch_id)
            for key, reply in replies.items():
                # A call settles its row with the answer, but for a row given up on before: a
                # reply settles it still, anything else is its call's on a row of its own.
                if key in batch.waiting and (
                    isinstance(reply, Reply) or batch.rows.get(key) not in self.unsettled
                ):
                    continue
                if isinstance(reply, ProviderError):
                    await self.journal.answered(batch_id, key, None, f"account: {reply}")
                    await self.journal.account_error(self.provider.name, f"account: {reply}")
                else:
                    await self.journal.answered(batch_id, key, reply and reply.usage)
        del self._open[batch_id]
        for key in batch.waiting:  # None too for a request the results did not name
            self._resolve(key, replies.get(key))

    async def _bounded[T](self, operation: Coroutine[None, None, T]) -> T:
        """A provider operation, failing (as the provider's own errors do) past `operation`."""
        return await asyncio.wait_for(operation, self.operation)

    def _give_up(self, batch: _Open) -> None:
        """Its waiting calls go as normal calls while it may still answer them."""
        self.unsettled.update(batch.rows[key] for key in batch.waiting if key in batch.rows)

    def _resolve(self, key: str, reply: Answered) -> None:
        future = self._futures.pop(key, None)
        if future is not None and not future.done():
            future.set_result(reply)
