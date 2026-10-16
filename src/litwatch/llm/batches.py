"""Message Batches inside the gateway (docs/specs/v1/models.md, batches). A batching gateway hands
each call it cannot reuse to its provider's `Batcher`, which holds the calls until none has joined
for a while, sends them as one batch, polls it, and answers each held call with its reply. A call
it cannot answer from a batch (the deadline passed, the batch did not answer it, the provider
failed) gets None, and the gateway sends it as a normal call."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from litwatch.config import RoleConfig
from litwatch.llm.types import Reply, Request, Usage

log = logging.getLogger(__name__)

QUIET_SECONDS = 10.0  # a batch goes out once no call has joined it for this long
MAX_REQUESTS = 10_000  # or once it holds this many (Anthropic allows 100,000)
MAX_BYTES = 200_000_000  # or this much (Anthropic allows 256 MB)
POLL_SECONDS = 60.0
DRAIN = timedelta(minutes=30)  # how long cancelled batches are waited for after the deadline


@runtime_checkable
class BatchProvider(Protocol):
    """A provider that batches (anthropic.AnthropicProvider). `custom_id` is the request key."""

    async def submit(self, requests: list[tuple[str, RoleConfig, Request]]) -> str: ...
    async def status(self, batch_id: str) -> str: ...
    async def cancel(self, batch_id: str) -> None: ...
    async def results(self, batch_id: str) -> dict[str, Reply | None]: ...


class BatchJournal(Protocol):
    async def batched(self, call_ids: list[int], batch_id: str) -> None: ...
    async def answered(self, batch_id: str, key: str, usage: Usage) -> None: ...


@dataclass
class _Held:
    key: str
    role: RoleConfig
    request: Request
    call_id: int | None
    size: int


@dataclass
class _Open:
    """A batch sent: the futures awaiting its answers, by request key."""

    waiting: dict[str, asyncio.Future[Reply | None]] = field(default_factory=dict)
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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.provider, self.journal, self.deadline = provider, journal, deadline
        self.quiet, self.poll, self.drain, self.now = quiet, poll, drain, now
        self.max_requests, self.max_bytes = max_requests, max_bytes
        self._held: list[_Held] = []
        self._futures: dict[str, asyncio.Future[Reply | None]] = {}  # held or sent, by key
        self._open: dict[str, _Open] = {}
        self._joined = 0.0  # when the last call joined the held ones (monotonic)
        self._flusher: asyncio.Task[None] | None = None
        self._poller: asyncio.Task[None] | None = None

    def open(self) -> bool:
        return self.now() < self.deadline

    async def ask(
        self, key: str, role: RoleConfig, request: Request, call_id: int | None
    ) -> Reply | None:
        """The reply from a batch, or None: send it as a normal call. An identical call held
        or sent already shares its request, and its reply at no cost."""
        if not self.open():
            return None
        if key in self._futures:
            return await self._shared(self._futures[key])
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        size = len(request.system) + len(request.user) + 2_000  # the schema and the envelope
        self._held.append(_Held(key, role, request, call_id, size))
        self._joined = time.monotonic()
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._flush_when_quiet())
        return await asyncio.shield(future)

    async def attach(self, batch_id: str, key: str) -> Reply | None:
        """The reply of a call a dead run sent in batch `batch_id`."""
        if key in self._futures:
            return await self._shared(self._futures[key])
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        self._open.setdefault(batch_id, _Open()).waiting[key] = future
        self._start_polling()
        return await asyncio.shield(future)

    @staticmethod
    async def _shared(future: asyncio.Future[Reply | None]) -> Reply | None:
        reply = await asyncio.shield(future)
        if reply is None:
            return None
        usage = reply.usage.model_copy(
            update={"cost": 0.0, "reused": True, "original_cost": reply.usage.cost}
        )
        return dataclasses.replace(reply, usage=usage)

    def watch(self, batch_id: str) -> None:
        """Read batch `batch_id` once it ends, so the charges of its answers no call awaits are
        journalled (a dead run's batch)."""
        self._open.setdefault(batch_id, _Open())
        self._start_polling()

    async def close(self) -> None:
        """Stop collecting and polling; whatever still waits is sent as normal calls."""
        for task in (self._flusher, self._poller):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._resolve_all(None)

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
            take, self._held = self._held[: self.max_requests], self._held[self.max_requests :]
            await self._send(take)

    async def _send(self, held: list[_Held]) -> None:
        if not self.open():
            for h in held:
                self._resolve(h.key, None)
            return
        try:
            batch_id = await self.provider.submit([(h.key, h.role, h.request) for h in held])
        except Exception:
            log.exception(
                "a batch of %d calls could not be sent; they go as normal calls", len(held)
            )
            for h in held:
                self._resolve(h.key, None)
            return
        log.info("batch %s sent with %d calls", batch_id, len(held))
        if self.journal:
            ids = [h.call_id for h in held if h.call_id is not None]
            await self.journal.batched(ids, batch_id)
        self._open[batch_id] = _Open({h.key: self._futures[h.key] for h in held})
        self._start_polling()

    # -- polling -------------------------------------------------------------------------------

    def _start_polling(self) -> None:
        if self._poller is None or self._poller.done():
            self._poller = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while self._open:
            now = self.now()
            if now >= self.deadline + self.drain:
                log.warning("%d batches still open after the deadline's drain", len(self._open))
                for batch_id in list(self._open):
                    for key in self._open.pop(batch_id).waiting:
                        self._resolve(key, None)
                return
            for batch_id, batch in list(self._open.items()):
                await self._check(batch_id, batch, now)
            if self._open:
                await asyncio.sleep(self.poll)

    async def _check(self, batch_id: str, batch: _Open, now: datetime) -> None:
        try:
            if await self.provider.status(batch_id) != "ended":
                if now >= self.deadline and not batch.cancelled:
                    await self.provider.cancel(batch_id)
                    batch.cancelled = True
                    log.info("batch %s cancelled at the deadline", batch_id)
                return
            replies = await self.provider.results(batch_id)
        except Exception:
            log.exception("batch %s could not be read; trying again", batch_id)
            return
        del self._open[batch_id]
        for key, reply in replies.items():
            if key in batch.waiting:
                self._resolve(key, reply)
            elif reply is not None and self.journal:
                await self.journal.answered(batch_id, key, reply.usage)
        for key in batch.waiting:  # a request the results did not name
            self._resolve(key, None)

    def _resolve(self, key: str, reply: Reply | None) -> None:
        future = self._futures.pop(key, None)
        if future is not None and not future.done():
            future.set_result(reply)

    def _resolve_all(self, reply: Reply | None) -> None:
        self._held = []
        self._open = {}
        for key in list(self._futures):
            self._resolve(key, reply)
