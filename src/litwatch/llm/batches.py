"""Batches inside the Model gate (docs/specs/v1/models.md, batches): Anthropic's Message
Batches, OpenAI's Batch API. The gate keeps a `Batcher` per provider and deadline, shared by
every item whose calls have that deadline. It holds the calls it is handed until none has joined
for a while, sends them as one batch (one per model, for a provider whose batches hold one),
polls it, and answers each held call with its reply, or with the account error the provider
gave the request instead, which is never asked again. A call it cannot answer from a batch (the
deadline passed, the batch did not answer it, the provider failed) gets None, and the gateway
sends it as a normal call, on rows of its own.

A batch's calls' rows open together, as the batch is about to be created; its outcome is
settled once, in one journal operation, before any call is answered (`CallJournal.settle_batch`):
each request's charge, its refusal or truncation, its failure, and what the batch did not answer.
The call is given its row's id with its reply (`Usage.source_call_id`), on which it saves only
its output or why that could not be used. A batch given up on while still open (the drain ran
out, the gate closed) may still answer: its rows are left for the next process to account for
(`ModelGate.recover`). The batcher's tasks serve many items, so they run outside any item's
context, and the journal's units under the gate's own attribution.

A batch is known by its id, or before it has one by the reference its provider staged
(`STAGED`), on its rows: a submission whose outcome is unknown (a timeout, a lost connection, a
5xx), and a dead run's staged rows alike, are looked for for a while from when the batch was
first known so (`FIND_SECONDS`), by a provider that can find one. Found, the batch's id goes on
its rows before it is polled; not found, its calls go as normal calls, at the risk of paying for
both. A submission the provider rejected (any other error) made no batch, and charged nothing."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from litwatch.config import RoleConfig
from litwatch.core.budget import CallOutcome, CallStart
from litwatch.llm.types import ProviderError, Reply, Request, Stop, Usage

log = logging.getLogger(__name__)

QUIET_SECONDS = 10.0  # a batch goes out once no call has joined it for this long
MAX_REQUESTS = 10_000  # or once it holds this many (Anthropic allows 100,000, OpenAI 50,000)
MAX_BYTES = 200_000_000  # or this much (Anthropic allows 256 MB, OpenAI a 200 MB file)
POLL_SECONDS = 60.0
OPERATION_SECONDS = 600.0  # a submission, status, cancel or results read, as a whole
DRAIN = timedelta(minutes=30)  # how long cancelled batches are waited for after the deadline
# How long a batch known by its staged reference is looked for, lookups and the waits between
# them (`FIND_DELAYS`) all inside: a batch shows in its provider's list within seconds, so two
# minutes cover a slow one, while its calls wait no longer than a submission may take
# (`OPERATION_SECONDS`).
FIND_SECONDS = 120.0
FIND_DELAYS = (5.0, 15.0, 30.0, 60.0)
NOT_ANSWERED = "the batch did not answer it"
STAGED = "staged:"  # a batch known by the reference its provider staged, as its rows name it
# What a request was charged that its provider did not bill: merged into its row's own usage,
# which keeps its model (`CallJournal`).
UNBILLED = Usage(model="", cost=0.0)

# What a batch gives a request: its reply; a provider error, the account's or one the provider
# billed (its `usage`); or None (not answered). A call is given a reply, an account error or None.
type Answered = Reply | ProviderError | None
# Told just before the provider creates the batch, with the reference it can be found by if it
# has one (`BatchProvider.submit`); it raises when the batch must not be created.
type Staged = Callable[[str | None], Awaitable[None]]


@runtime_checkable
class BatchProvider(Protocol):
    """A provider that batches (`anthropic.AnthropicProvider`, `openai.OpenAIProvider`).
    `custom_id` is the request key. `one_model`: each batch holds one model's requests only.
    `submit` gives the batch's id, calling `staged` just before it creates the batch, with a
    reference `find` can find it by, if any; it raises `unanswered` (or `timeout`) when the
    creation's outcome is unknown. `find` gives the id of the batch made with a reference, or
    None when it finds none (yet); a provider that cannot find one has `find = None`."""

    name: str
    one_model: bool
    find: Callable[[str | None], Coroutine[None, None, str | None]] | None

    async def submit(
        self, requests: list[tuple[str, RoleConfig, Request]], staged: Staged
    ) -> str: ...
    async def status(self, batch_id: str) -> str: ...  # in_progress, canceling, ended or gone
    async def cancel(self, batch_id: str) -> None: ...
    async def results(self, batch_id: str) -> dict[str, Answered]: ...


class BatchJournal(Protocol):
    """Where a batch's calls are opened and settled (`store.calls.CallJournal`, the gate's)."""

    async def start_many(
        self, calls: list[CallStart], batch_id: str | None = None
    ) -> list[int]: ...
    async def batched(self, call_ids: list[int], batch_id: str, provider: str) -> None: ...
    async def settle_batch(
        self,
        batch_id: str,
        answers: dict[str, CallOutcome],
        *,
        rest: str | None = None,
        account: tuple[str, str] | None = None,
    ) -> None: ...
    async def adopted(self, reference: str, batch_id: str) -> None: ...
    async def unsent(self, call_ids: list[int], outcome: CallOutcome) -> None: ...
    async def account_error(self, provider: str, error: str) -> None: ...


_EXPIRED = "provider: the batch was not made: its deadline passed"
_GONE = "the batch is gone"
_MAY_HAVE_BEEN_MADE = "provider: the batch may have been made"


class _ExpiredError(Exception):
    """The deadline passed before the batch was created: its calls go as normal calls."""


def _unknown(exc: BaseException) -> bool:
    """Whether a submission that failed may still have made its batch: it timed out (ours or
    the provider's) or its reply was lost, not refused by the provider."""
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, ProviderError) and not exc.account and (exc.unanswered or exc.timeout)


def _given(answer: Answered) -> Answered:
    """What a call is given of a request's answer: a reply, an account error, else None."""
    if isinstance(answer, ProviderError) and not answer.account:
        return None
    return answer


def _outcome(answer: Answered) -> CallOutcome:
    """How a request's answer settles its row: a reply's charge, with its refusal or truncation,
    which need no validating; a failure, with what it was billed (an account error and a request
    the batch named and did not answer were billed nothing); its charge unknown otherwise."""
    if isinstance(answer, Reply):
        error = None
        if answer.stop == Stop.REFUSED:
            error = f"refused: {answer.refusal}" if answer.refusal else "refused"
        elif answer.stop == Stop.TRUNCATED:
            error = "truncated"
        return CallOutcome(usage=answer.usage, error=error, replied=True)
    if isinstance(answer, ProviderError):
        usage = answer.usage or (UNBILLED if answer.account else None)
        return CallOutcome(
            usage=usage, error=f"{'account' if answer.account else 'provider'}: {answer}"
        )
    return CallOutcome(usage=UNBILLED, error=f"provider: {NOT_ANSWERED}")


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
    start: CallStart  # its row, opened as its batch is created
    size: int


@dataclass
class _Open:
    """A batch sent: the futures awaiting its answers, and the journal rows its requests are
    on, by request key."""

    waiting: dict[str, asyncio.Future[Answered]] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)
    cancelled: bool = False
    # Known by its staged reference: when looking for it ends (monotonic), from when it was
    # first known so.
    find_until: float | None = None


class Batcher:
    """One provider's batches until `deadline` (UTC), for every call with that deadline."""

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
        find_seconds: float = FIND_SECONDS,
        find_delays: tuple[float, ...] = FIND_DELAYS,
        operation: float = OPERATION_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.provider, self.journal, self.deadline = provider, journal, deadline
        self.quiet, self.poll, self.drain, self.now = quiet, poll, drain, now
        self.max_requests, self.max_bytes, self.operation = max_requests, max_bytes, operation
        self.find_seconds, self.find_delays = find_seconds, find_delays
        self._held: list[_Held] = []
        self._futures: dict[str, asyncio.Future[Answered]] = {}  # held or sent, by key
        self._open: dict[str, _Open] = {}
        self._joined = 0.0  # when the last call joined the held ones (monotonic)
        self._flusher: asyncio.Task[None] | None = None
        self._poller: asyncio.Task[None] | None = None

    def open(self) -> bool:
        return self.now() < self.deadline

    def holds(self, batch_id: str) -> bool:
        """Whether this batcher polls batch `batch_id`."""
        return batch_id in self._open

    def idle(self) -> bool:
        """Whether it holds, polls and runs nothing, past its deadline's drain: done for good."""
        running = any(t is not None and not t.done() for t in (self._flusher, self._poller))
        late = self.now() >= self.deadline + self.drain
        return late and not (self._held or self._open or self._futures or running)

    async def ask(self, key: str, role: RoleConfig, request: Request, start: CallStart) -> Answered:
        """The reply from a batch, its account error, or None: send it as a normal call. Its
        journal row opens as `start` says as its batch is sent. An identical call held or sent
        already shares its request, and what it is given, a reply at no cost."""
        if not self.open():
            return None
        if key in self._futures:
            return await self._shared(self._futures[key])
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        size = _size(request)
        self._held.append(_Held(key, role, request, start, size))
        self._joined = time.monotonic()
        if self._flusher is None or self._flusher.done():
            self._flusher = self._supervised(self._flush_when_quiet())
        return await asyncio.shield(future)

    async def attach(self, batch_id: str, key: str, call_id: int) -> Answered:
        """The reply of a call a dead run sent in batch `batch_id` (an id, or a `STAGED`
        reference), on journal row `call_id`."""
        if key in self._futures:
            return await self._shared(self._futures[key])
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        batch = self._opened(batch_id)
        batch.waiting[key], batch.rows[key] = future, call_id
        self._start_polling()
        return await asyncio.shield(future)

    def _opened(self, batch_id: str) -> _Open:
        """Batch `batch_id` as this batcher polls it; one known by its staged reference is
        looked for from now, when it is first known here."""
        if batch_id not in self._open:
            staged = batch_id.startswith(STAGED)
            self._open[batch_id] = _Open(
                find_until=time.monotonic() + self.find_seconds if staged else None
            )
        return self._open[batch_id]

    @staticmethod
    async def _shared(future: asyncio.Future[Answered]) -> Answered:
        """What an identical call shares: what its first call is given, a reply at no cost and
        on its first call's row (`source_call_id`)."""
        reply = await asyncio.shield(future)
        if not isinstance(reply, Reply):
            return reply
        usage = reply.usage.model_copy(
            update={"cost": 0.0, "reused": True, "original_cost": reply.usage.cost}
        )
        return dataclasses.replace(reply, usage=usage)

    def watch(self, batch_id: str) -> None:
        """Read batch `batch_id` (an id, or a `STAGED` reference) once it ends, so the charges of
        its answers no call awaits are journalled (a dead run's batch)."""
        self._opened(batch_id)
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
        self._held, self._open = [], {}
        for key in list(self._futures):
            self._resolve(key, None)

    def _supervised(self, work: Coroutine[None, None, None]) -> asyncio.Task[None]:
        """A background task whose failure (the journal's, say) fails every call still
        waiting, rather than leaving them waiting for good. It serves every item with this
        deadline, so it runs in a context of its own, outside the item whose call started it."""

        def failed(task: asyncio.Task[None]) -> None:
            if task.cancelled() or (exc := task.exception()) is None:
                return
            log.error("batches stopped: %r", exc)
            self._held, self._open = [], {}
            for future in self._futures.values():
                if not future.done():
                    future.set_exception(exc)
            self._futures = {}

        task = asyncio.create_task(work, context=contextvars.Context())
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
            await self._send(self._take())

    def _take(self) -> list[_Held]:
        """The next batch, taken from the held calls in order: within both ceilings, of one call
        at least, and of the first call's model where a batch holds one model only."""
        first = self._held[0].role.model
        take: list[_Held] = []
        size = 0
        for h in self._held:
            if self.provider.one_model and h.role.model != first:
                continue
            if len(take) == self.max_requests or (take and size + h.size > self.max_bytes):
                break
            take.append(h)
            size += h.size
        taken = {id(h) for h in take}
        self._held = [h for h in self._held if id(h) not in taken]
        return take

    async def _send(self, held: list[_Held]) -> None:
        rows: list[int] = []
        handle: str | None = None  # the batch's staged reference, as its rows name it
        creating = False  # whether its creation was asked for: before that, none can be made

        async def staged(reference: str | None) -> None:
            """Just before the batch is created: past the deadline it is not; else its calls'
            rows are opened, together, naming its reference, and the deadline looked at again,
            as opening them takes time."""
            nonlocal handle, creating
            if not self.open():
                raise _ExpiredError
            handle = None if reference is None else STAGED + reference
            if self.journal:
                rows[:] = await self.journal.start_many([h.start for h in held], handle)
            if not self.open():
                await self._unsent(rows, CallOutcome(usage=UNBILLED, error=_EXPIRED))
                raise _ExpiredError
            creating = True

        requests = [(h.key, h.role, h.request) for h in held]
        try:
            if not self.open():
                raise _ExpiredError
            batch_id = await self._bounded(self.provider.submit(requests, staged))
        except _ExpiredError:
            for h in held:
                self._resolve(h.key, None)
            return
        except Exception as exc:
            unknown = creating and _unknown(exc)
            if not (unknown and handle and self.provider.find):
                await self._not_sent(held, rows, handle, exc, unknown=unknown)
                return
            log.warning("batch %s may have been made (%s); looking for it", handle, exc)
            batch_id = handle  # found by `_check`, as a dead run's is
        else:
            log.info("batch %s sent with %d calls", batch_id, len(held))
            if self.journal:
                await self.journal.batched(rows, batch_id, held[0].role.provider)
        batch = self._opened(batch_id)
        batch.waiting |= {h.key: self._futures[h.key] for h in held}
        batch.rows |= dict(zip((h.key for h in held), rows, strict=False))
        self._start_polling()

    async def _unsent(self, rows: list[int], outcome: CallOutcome) -> None:
        if self.journal and rows:
            await self.journal.unsent(rows, outcome)

    async def _not_sent(
        self,
        held: list[_Held],
        rows: list[int],
        handle: str | None,
        exc: Exception,
        *,
        unknown: bool,
    ) -> None:
        """A submission that failed and made no batch, or (`unknown`) may have made one that
        cannot be found: its calls' rows settled (charged nothing, or an unknown charge for one
        that may have been made), the calls given the account's error or None (a normal call)."""
        identity = handle or f"of {held[0].key[:12]}…"
        account = isinstance(exc, ProviderError) and exc.account
        if account:  # never asked again: the calls fail on the account
            log.error("a batch of %d calls was refused for the account: %s", len(held), exc)
            outcome = CallOutcome(usage=UNBILLED, error=f"account: {exc}")
            if self.journal:
                await self.journal.account_error(self.provider.name, f"account: {exc}")
        elif unknown:
            log.warning(
                "batch %s may have been made and cannot be found; its %d calls go as normal "
                "calls, which may pay for them twice: %s",
                identity,
                len(held),
                exc,
            )
            outcome = CallOutcome(error=f"provider: the batch may have been made: {exc}")
        else:
            log.warning("batch %s was not made; its calls go as normal calls: %r", identity, exc)
            outcome = CallOutcome(usage=UNBILLED, error=f"provider: the batch was not made: {exc}")
        await self._unsent(rows, outcome)
        for h in held:
            self._resolve(h.key, exc if account else None)

    async def _find(self, reference: str, end: float) -> str | None:
        """The id of the batch made with `reference`, looked for until `end` (monotonic),
        lookups and the waits between them included; None when not found by then, at once for a
        provider that cannot find one. An account error is raised."""
        if (find := self.provider.find) is None:
            return None
        for delay in self.find_delays:
            await asyncio.sleep(max(0.0, min(delay, end - time.monotonic())))
            if (left := end - time.monotonic()) <= 0:
                break
            try:
                if (batch_id := await asyncio.wait_for(find(reference), left)) is not None:
                    return batch_id
            except ProviderError as exc:
                if exc.account:
                    raise
                log.warning("looking for batch %s failed: %s", reference, exc)
            except TimeoutError:
                break
        return None

    async def _adopt(self, handle: str, batch: _Open) -> None:
        """A batch known by its staged reference: once found, its id on its rows, and polled by
        it; not found, its calls go as normal calls. An account error fails them, its rows left
        to recover."""
        assert batch.find_until is not None
        try:
            batch_id = await self._find(handle.removeprefix(STAGED), batch.find_until)
        except ProviderError as exc:
            log.error("batch %s could not be looked for, for the account: %s", handle, exc)
            if self.journal:
                await self.journal.account_error(self.provider.name, f"account: {exc}")
            del self._open[handle]
            for key in batch.waiting:
                self._resolve(key, exc)
            return
        if batch_id is None:
            log.warning(
                "batch %s may have been made and was not found; its calls go as normal calls, "
                "which may pay for them twice",
                handle,
            )
            if self.journal:
                await self.journal.settle_batch(handle, {}, rest=_MAY_HAVE_BEEN_MADE)
            del self._open[handle]
            for key in batch.waiting:
                self._resolve(key, None)
            return
        log.warning("batch %s found as %s", handle, batch_id)
        if self.journal:  # before it is polled, so a run that dies meanwhile reads it by its id
            await self.journal.adopted(handle, batch_id)
        del self._open[handle]
        found = self._opened(batch_id)
        found.waiting |= batch.waiting
        found.rows |= batch.rows

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
                    for key in batch.waiting:
                        self._resolve(key, None)
                continue  # batches attached meanwhile, if any, are looked at now
            if self._open:
                await asyncio.sleep(self.poll)

    async def _check(self, batch_id: str, batch: _Open, now: datetime) -> None:
        if batch_id.startswith(STAGED):
            await self._adopt(batch_id, batch)
            return
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
            # open on its rows for a later run to account for. Seen here, once, whether or not
            # a call waits on it (a dead run's batch): the provider is failed for its account.
            log.error("batch %s could not be read for the account: %s", batch_id, exc)
            if self.journal:
                await self.journal.account_error(self.provider.name, f"account: {exc}")
            del self._open[batch_id]
            for key in batch.waiting:
                self._resolve(key, exc)
            return
        if status == "gone":  # deleted, or another workspace's: it never answers
            log.warning("batch %s is gone from its provider", batch_id)
        if self.journal:  # the batch is accounted for, once, before it is let go
            # What the results named no answer for ends here; a batch gone answers nothing.
            rest = {"ended": f"provider: {NOT_ANSWERED}", "gone": f"provider: {_GONE}"}[status]
            account = next(
                (
                    f"account: {r}"
                    for r in replies.values()
                    if isinstance(r, ProviderError) and r.account
                ),
                None,
            )
            await self.journal.settle_batch(
                batch_id,
                {key: _outcome(reply) for key, reply in replies.items()},
                rest=rest,
                account=None if account is None else (self.provider.name, account),
            )
        del self._open[batch_id]
        for key in batch.waiting:  # None too for a request the results did not name
            reply = replies.get(key)
            if isinstance(reply, Reply) and key in batch.rows:  # on that row, shared or not
                reply.usage.source_call_id = batch.rows[key]
            self._resolve(key, _given(reply))

    async def _bounded[T](self, operation: Coroutine[None, None, T]) -> T:
        """A provider operation, failing (as the provider's own errors do) past `operation`."""
        return await asyncio.wait_for(operation, self.operation)

    def _resolve(self, key: str, reply: Answered) -> None:
        future = self._futures.pop(key, None)
        if future is not None and not future.done():
            future.set_result(reply)
