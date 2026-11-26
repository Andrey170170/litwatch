"""Batches inside the Model gate (docs/specs/v1/models.md, batches): Anthropic's Message
Batches, OpenAI's Batch API. The gate keeps a `Batcher` per provider and deadline, shared by
every item whose calls have that deadline. It holds the calls it is handed until none has joined
for a while, sends them as one batch (one per model, for a provider whose batches hold one),
and follows each batch it sends or is handed (a dead run's) in a task of its own to its
settlement, which answers each call waiting on it with its reply, or with the account error the
provider gave the request instead, which is never asked again; one batch's failure fails only
the calls waiting on it. A call it cannot answer from a batch (the
deadline passed, the batch did not answer it, the provider failed) gets None, and the gateway
sends it as a normal call, on rows of its own.

A batch's calls' rows open together, as the batch is about to be created; its outcome is
settled once, in one journal transaction with the totals of the runs it reached, and its calls
answered once that has committed (`CallJournal.settle_batch`): each request's charge, its
refusal or truncation, its failure, and what the batch did not answer.
The call is given its row's id with its reply (`Usage.source_call_id`), on which it saves only
its output or why that could not be used. A batch given up on while still open (the drain ran
out, the gate closed) may still answer: its rows are left for the next process to account for
(`ModelGate.recover`). The batcher's tasks serve many items, so they run outside any item's
context, and the journal's units under the gate's own attribution.

A batch is known by its id, or before it has one by the reference its provider staged
(`STAGED`), on its rows: a submission whose outcome is unknown (a timeout, a lost connection, a
5xx), and a dead run's staged rows alike, are looked for at once, for a while from when the
batch was first known so (`FIND_SECONDS`), by a provider that can find one. Found, the batch's
id goes on its rows before it is polled; not found, its calls go as normal calls, at the risk of
paying for both. A submission the provider rejected (any other error) made no batch, and
charged nothing."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from litwatch.config import RoleConfig
from litwatch.core.budget import CallOutcome, CallStart
from litwatch.gates.work import retryable
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
# The waits before a batch's journal write is tried again, the database briefly away.
JOURNAL_DELAYS = (1.0, 5.0, 20.0, 60.0)
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
        self,
        calls: list[CallStart],
        batch_id: str | None = None,
        opened: Callable[[list[int]], None] | None = None,
    ) -> list[int]: ...
    async def batched(self, call_ids: list[int], batch_id: str, provider: str) -> None: ...
    async def settle_batch(
        self, batch_id: str, answers: dict[str, CallOutcome], *, rest: str | None = None
    ) -> None: ...
    async def adopted(self, reference: str, batch_id: str) -> None: ...
    async def named(self, batch: int) -> str | None: ...
    async def unsent(
        self, call_ids: list[int], outcome: CallOutcome, *, account: str | None = None
    ) -> None: ...
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


def _outcome(answer: Answered, debug: bool = False) -> CallOutcome:
    """How a request's answer settles its row: a reply's charge, with its refusal or truncation,
    which need no validating (in `debug` mode, with its text); a failure, with what it was
    billed (an account error and a request the batch named and did not answer were billed
    nothing); its charge unknown otherwise."""
    if isinstance(answer, Reply):
        error = None
        if answer.stop == Stop.REFUSED:
            error = f"refused: {answer.refusal}" if answer.refusal else "refused"
        elif answer.stop == Stop.TRUNCATED:
            error = "truncated"
        kept = (answer.raw or answer.text) if debug and error else None
        return CallOutcome(usage=answer.usage, error=error, replied=True, reply=kept)
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
    waiters: int = 0  # the calls waiting on it: the last to stop waiting takes it out


@dataclass
class _Batch:
    """A batch's one owner record in the process: the batcher that follows it, in a task of its
    own, from its creation or adoption to its settlement. The futures awaiting its answers and
    the journal rows its requests are on, by request key; its identity, its first call's id,
    which never changes from its rows' opening on (`store.calls.CallJournal.in_batch`), and the
    name it goes by now (its staged reference, then its provider's id). Its own `deadline`, at
    which it is cancelled and from which its drain runs: its sender's, or the latest of the
    calls that wait on it with one (a nightly started after its own). A call without a deadline
    keeps its own limit, one look: it waits on the batch's next look (`lookers`), which it wakes
    it for (`woken`), and is given None when that look does not end it, at once while the batch
    is being created (`sending`)."""

    deadline: datetime
    waiting: dict[str, asyncio.Future[Answered]] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)
    identity: int | None = None
    name: str | None = None
    lookers: list[asyncio.Future[None]] = field(default_factory=list)
    woken: asyncio.Event = field(default_factory=asyncio.Event)
    sending: bool = False
    cancelled: bool = False
    # Known by its staged reference: when looking for it ends (monotonic), from when its
    # creation's outcome was known to be unknown, or its name was read again here.
    find_until: float | None = None


class Batcher:
    """One provider's batches until `deadline` (UTC), for every call with that deadline: one
    task collects the calls handed to it, and each batch it sends, or is handed to follow, is
    owned by a task of its own (`_supervised`) through its settlement, so no batch's lookups,
    reads or failures hold up or fail another's. The batches are known here by their identity
    (`_Batch`), so a call that finds one by either of its names joins its one task. Each batch
    keeps its own deadline (`_Batch.deadline`), this batcher's for those it sends. A journal
    write after the provider has answered is tried again while the database is briefly away
    (`JOURNAL_DELAYS`), the answer kept. In `debug` mode a batched refusal's or truncation's
    text is kept on its row."""

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
        journal_delays: tuple[float, ...] = JOURNAL_DELAYS,
        debug: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.provider, self.journal, self.deadline = provider, journal, deadline
        self.quiet, self.poll, self.drain, self.now = quiet, poll, drain, now
        self.max_requests, self.max_bytes, self.operation = max_requests, max_bytes, operation
        self.find_seconds, self.find_delays, self.debug = find_seconds, find_delays, debug
        self.journal_delays = journal_delays
        self._held: dict[str, _Held] = {}  # in the order they joined
        self._futures: dict[str, asyncio.Future[Answered]] = {}  # held or sent, by key
        self._batches: dict[int, _Batch] = {}  # by identity
        self._tasks: set[asyncio.Task[None]] = set()  # one per batch, to its settlement
        self._joined = 0.0  # when the last call joined the held ones (monotonic)
        self._flusher: asyncio.Task[None] | None = None

    def open(self) -> bool:
        return self.now() < self.deadline

    def holds(self, batch: int) -> bool:
        """Whether this batcher follows the batch of identity `batch`."""
        return batch in self._batches

    def followed(self) -> list[int]:
        """The identities of the batches this batcher follows."""
        return list(self._batches)

    def idle(self) -> bool:
        """Whether it holds, follows and runs nothing, past its deadline's drain: done for
        good."""
        collecting = self._flusher is not None and not self._flusher.done()
        late = self.now() >= self.deadline + self.drain
        return late and not (self._held or self._tasks or self._futures or collecting)

    async def ask(self, key: str, role: RoleConfig, request: Request, start: CallStart) -> Answered:
        """The reply from a batch, its account error, or None: send it as a normal call. Its
        journal row opens as `start` says as its batch is sent. An identical call held or sent
        already shares its request, and what it is given, a reply at no cost. A request every
        call stopped waiting on while it was held is not sent."""
        if not self.open():
            return None
        if (future := self._futures.get(key)) is not None:
            return await self._interested(key, self._shared(future))
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        self._held[key] = _Held(key, role, request, start, _size(request))
        self._joined = time.monotonic()
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(
                self._flush_when_quiet(), context=contextvars.Context()
            )
            self._flusher.add_done_callback(self._collecting_ended)
        return await self._interested(key, asyncio.shield(future))

    async def _interested(self, key: str, answer: Awaitable[Answered]) -> Answered:
        """`answer`, awaited by one of the calls that want `key`'s: while it is still held, the
        last of them to stop waiting (its lease lost, say) takes it out, so it is never sent."""
        held = self._held.get(key)
        if held is not None:
            held.waiters += 1
        try:
            return await answer
        finally:
            if held is not None and self._held.get(key) is held:
                held.waiters -= 1
                if held.waiters == 0:
                    del self._held[key]
                    if (future := self._futures.pop(key, None)) and not future.done():
                        future.cancel()

    async def attach(
        self, batch: int, name: str, key: str, call_id: int, *, until: datetime | None
    ) -> Answered:
        """The reply of a call sent in the batch of identity `batch`, going by `name` (an id,
        or a `STAGED` reference), on journal row `call_id`, waited for as the caller's deadline
        `until` says: the batch is followed until it, or its own if later, and its drain (a
        nightly started after its deadline has one past); without one, None (a normal call)
        unless the batch's next look, which comes at once, ends it, the batch followed on as
        it was, or, followed by no one before, as at a deadline: cancelled at once, then
        drained."""
        followed = self._followed(batch, name, until or self.now())
        if until is not None:
            followed.deadline = max(followed.deadline, until)
        elif followed.sending:  # being created: no look of it can end it
            return None
        if (future := self._futures.get(key)) is not None:
            answer = self._interested(key, self._shared(future))
        else:
            future = asyncio.get_running_loop().create_future()
            self._futures[key] = future
            followed.waiting[key], followed.rows[key] = future, call_id
            answer = asyncio.shield(future)
        return await (answer if until is not None else self._once(followed, answer))

    @staticmethod
    async def _once(batch: _Batch, answer: Awaitable[Answered]) -> Answered:
        """`answer` if `batch`'s next look, which it is woken for, ends it; else None. The
        batch's task goes on as it would have: its answer still settles the row."""
        looked = asyncio.get_running_loop().create_future()
        batch.lookers.append(looked)
        batch.woken.set()
        answering = asyncio.ensure_future(answer)
        try:
            await asyncio.wait({answering, looked}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            answering.cancel()
            raise
        finally:
            looked.cancel()
        if answering.done():
            return answering.result()
        answering.cancel()
        return None

    def watch(self, batch: int, name: str) -> None:
        """Read the batch of identity `batch`, going by `name`, once it ends, so the charges of
        its answers no call awaits are journalled (a dead run's batch): under this batcher's
        deadline."""
        self._followed(batch, name, self.deadline)

    def _followed(self, identity: int, name: str, deadline: datetime) -> _Batch:
        """The batch of `identity` as this batcher follows it: when it is new here, under
        `deadline`, in a task of its own from now (`_taken`)."""
        if (batch := self._batches.get(identity)) is None:
            batch = _Batch(deadline=deadline, identity=identity, name=name)
            self._batches[identity] = batch
            self._supervised(batch, self._taken(batch))
        return batch

    async def _taken(self, batch: _Batch) -> None:
        """A batch followed from its rows (a dead run's, or one whose task here ended as a call
        read them), its identity claimed here, so a call that attaches meanwhile joins: the name
        it goes by is read again first, before any provider request, so it is followed by its id
        when its rows have one now, not looked for by the reference read before (its task here
        may have found and settled it meanwhile, and a lookup may miss it). Known still by its
        staged reference, it is looked for from then, for the whole window."""
        if (journal := self.journal) is not None and (identity := batch.identity) is not None:
            name = await self._journalled("batch named", lambda: journal.named(identity))
            batch.name = name or batch.name
        if batch.name is not None and batch.name.startswith(STAGED):
            batch.find_until = time.monotonic() + self.find_seconds
        await self._follow(batch)

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

    async def settle(self) -> None:
        """Wait until every batch is read, or given up on after the deadline's drain; one that
        failed has failed the calls waiting on it."""
        while self._tasks:  # and those followed meanwhile
            await asyncio.wait(set(self._tasks))

    async def close(self) -> None:
        """Stop collecting and following; whatever still waits is sent as normal calls."""
        tasks = [t for t in (self._flusher, *self._tasks) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._held, self._batches, self._tasks = {}, {}, set()
        for key, future in list(self._futures.items()):
            self._resolve(key, future, None)

    def _supervised(self, batch: _Batch, work: Coroutine[None, None, None]) -> None:
        """The task that owns `batch` to its settlement. Its failure (the journal's, past its
        retries) fails the calls waiting on that batch, and on no other, rather than leaving
        them waiting for good. It serves every item with this deadline, so it runs in a context
        of its own, outside the item whose call started it."""

        def ended(task: asyncio.Task[None]) -> None:
            self._tasks.discard(task)
            self._let_go(batch)
            if task.cancelled() or (exc := task.exception()) is None:
                return
            log.error("batch %s stopped: %r", batch.name or "unmade", exc)
            for key, future in batch.waiting.items():
                if self._futures.get(key) is future:
                    del self._futures[key]
                if not future.done():
                    future.set_exception(exc)

        task = asyncio.create_task(work, context=contextvars.Context())
        self._tasks.add(task)
        task.add_done_callback(ended)

    def _collecting_ended(self, task: asyncio.Task[None]) -> None:
        """The collector failed (it only takes and hands out batches): the calls it still held
        are sent as normal calls."""
        if task.cancelled() or task.exception() is None:
            return
        log.error("batch collecting stopped: %r", task.exception())
        held, self._held = self._held, {}
        for key in held:
            self._resolve(key, self._futures[key], None)

    def _let_go(self, batch: _Batch) -> None:
        """`batch` is no longer followed here: a call attached after is a new task's."""
        if batch.identity is not None and self._batches.get(batch.identity) is batch:
            del self._batches[batch.identity]

    def _answer(self, batch: _Batch, replies: Mapping[str, Answered]) -> None:
        """Let `batch` go and give each call waiting on it what it is given of its answer, on
        its row (`source_call_id`); None (a normal call) for a request `replies` lacks. Called
        once its settlement has committed, with no wait between the two, so a call attached
        after is a new task's."""
        self._let_go(batch)
        for key, future in batch.waiting.items():
            reply = replies.get(key)
            if isinstance(reply, Reply) and key in batch.rows:  # on that row, shared or not
                reply.usage.source_call_id = batch.rows[key]
            self._resolve(key, future, _given(reply))

    @staticmethod
    def _looked(batch: _Batch) -> None:
        """A look that did not end `batch`: the calls that look once are let go."""
        lookers, batch.lookers = batch.lookers, []
        for looked in lookers:
            if not looked.done():
                looked.set_result(None)

    def _resolve(self, key: str, future: asyncio.Future[Answered], reply: Answered) -> None:
        if self._futures.get(key) is future:
            del self._futures[key]
        if not future.done():
            future.set_result(reply)

    async def _journalled[T](self, what: str, write: Callable[[], Awaitable[T]]) -> T:
        return await journalled(what, write, self.journal_delays)

    # -- collecting ----------------------------------------------------------------------------

    def _full(self) -> bool:
        return (
            len(self._held) >= self.max_requests
            or sum(h.size for h in self._held.values()) >= self.max_bytes
        )

    async def _flush_when_quiet(self) -> None:
        while self._held:
            left = self._joined + self.quiet - time.monotonic()
            if left > 0 and not self._full() and self.open():
                await asyncio.sleep(min(left, 1.0))
                continue
            held = self._take()
            batch = _Batch(self.deadline, waiting={h.key: self._futures[h.key] for h in held})
            self._supervised(batch, self._sent(batch, held))

    def _take(self) -> list[_Held]:
        """The next batch, taken from the held calls in order: within both ceilings, of one call
        at least, and of the first call's model where a batch holds one model only."""
        first = next(iter(self._held.values())).role.model
        take: list[_Held] = []
        size = 0
        for h in self._held.values():
            if self.provider.one_model and h.role.model != first:
                continue
            if len(take) == self.max_requests or (take and size + h.size > self.max_bytes):
                break
            take.append(h)
            size += h.size
        for h in take:
            del self._held[h.key]
        return take

    # -- a batch, from its creation to its settlement --------------------------------------------

    async def _sent(self, batch: _Batch, held: list[_Held]) -> None:
        """`batch`'s task from its creation: made of `held`, or, when its creation's outcome is
        unknown, looked for from then; then followed to its settlement (`_follow`). Its rows
        open together just before it is made, and it is known here by its identity before they
        are committed, so a call that reads them finds it here, and from then until the
        provider has answered its creation it is being sent (`sending`)."""
        rows: list[int] = []
        handle: str | None = None  # the batch's staged reference, as its rows name it
        creating = False  # whether its creation was asked for: before that, none can be made

        def opened(ids: list[int]) -> None:
            """Its rows inserted, not yet committed: it is known here by its identity."""
            batch.rows = dict(zip((h.key for h in held), ids, strict=True))
            batch.identity, batch.name, batch.sending = min(ids), handle, True
            self._batches[batch.identity] = batch

        async def staged(reference: str | None) -> None:
            """Just before the batch is created: past the deadline it is not; else its calls'
            rows are opened, together, naming its reference, and the deadline looked at again,
            as opening them takes time."""
            nonlocal handle, creating
            if not self.open():
                raise _ExpiredError
            handle = None if reference is None else STAGED + reference
            if self.journal:
                try:
                    rows[:] = await self.journal.start_many([h.start for h in held], handle, opened)
                except BaseException:  # not opened: no call can find it
                    self._let_go(batch)
                    raise
            batch.name = handle
            if not self.open():
                await self._unsent(rows, CallOutcome(usage=UNBILLED, error=_EXPIRED))
                raise _ExpiredError
            creating = True

        requests = [(h.key, h.role, h.request) for h in held]
        try:
            if not self.open():
                raise _ExpiredError
            try:
                batch_id = await self._bounded(self.provider.submit(requests, staged))
            finally:
                batch.sending = False
        except _ExpiredError:
            self._answer(batch, {})
            return
        except Exception as exc:
            unknown = creating and _unknown(exc)
            if not (unknown and handle and self.provider.find):
                await self._not_sent(batch, rows, handle, exc, unknown=unknown)
                return
            log.warning("batch %s may have been made (%s); looking for it", handle, exc)
            batch.find_until = time.monotonic() + self.find_seconds
            await self._follow(batch)
            return
        log.info("batch %s sent with %d calls", batch_id, len(held))
        batch.name = batch_id
        if (journal := self.journal) is not None:
            provider = held[0].role.provider
            await self._journalled("batched", lambda: journal.batched(rows, batch_id, provider))
        await self._follow(batch)

    async def _unsent(
        self, rows: list[int], outcome: CallOutcome, account: str | None = None
    ) -> None:
        if (journal := self.journal) and rows:
            await self._journalled("unsent", lambda: journal.unsent(rows, outcome, account=account))

    async def _account_error(self, exc: ProviderError) -> None:
        if (journal := self.journal) is not None:
            error = f"account: {exc}"
            await self._journalled(
                "account error", lambda: journal.account_error(self.provider.name, error)
            )

    async def _not_sent(
        self,
        batch: _Batch,
        rows: list[int],
        handle: str | None,
        exc: Exception,
        *,
        unknown: bool,
    ) -> None:
        """A submission that failed and made no batch, or (`unknown`) may have made one that
        cannot be found: its calls' rows settled (charged nothing, or an unknown charge for one
        that may have been made), the calls given the account's error or None (a normal call)."""
        identity = handle or f"of {next(iter(batch.waiting))[:12]}…"
        account = exc if isinstance(exc, ProviderError) and exc.account else None
        if account is not None:  # never asked again
            calls = len(batch.waiting)
            log.error("a batch of %d calls was refused for the account: %s", calls, exc)
            outcome = CallOutcome(usage=UNBILLED, error=f"account: {exc}")
            if self.journal and rows:  # observed with the rows it settles
                await self._unsent(rows, outcome, self.provider.name)
            else:
                await self._account_error(account)
        elif unknown:
            log.warning(
                "batch %s may have been made and cannot be found; its %d calls go as normal "
                "calls, which may pay for them twice: %s",
                identity,
                len(batch.waiting),
                exc,
            )
            await self._unsent(rows, CallOutcome(error=f"{_MAY_HAVE_BEEN_MADE}: {exc}"))
        else:
            log.warning("batch %s was not made; its calls go as normal calls: %r", identity, exc)
            outcome = CallOutcome(usage=UNBILLED, error=f"provider: the batch was not made: {exc}")
            await self._unsent(rows, outcome)
        self._answer(batch, dict.fromkeys(batch.waiting, account))

    async def _follow(self, batch: _Batch) -> None:
        """Follow `batch` to its settlement: known by its staged reference, it is looked for
        first (`_adopt`); then looked at every `poll`, or at once for a call that looks once,
        until it ends, cancelled at its deadline, and given up on once its deadline's drain is
        over, looked at first."""
        assert batch.name is not None
        if batch.name.startswith(STAGED) and not await self._adopt(batch, batch.name):
            return
        while not await self._check(batch, self.now()):
            self._looked(batch)
            if self.now() >= batch.deadline + self.drain:
                # Left open on its rows, for the next process to account for (`recover`).
                log.warning("batch %s still open after the deadline's drain", batch.name)
                self._answer(batch, {})
                return
            batch.woken.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(batch.woken.wait(), self.poll)

    async def _find(self, batch: _Batch, reference: str, end: float) -> str | None:
        """The id of `batch`, made with `reference`, looked for until `end` (monotonic), lookups
        and the waits between them included; None when not found by then, at once for a
        provider that cannot find one. An account error is raised. Each lookup that does not
        find it is a look: a call that looks once is let go, and its wake (`woken`) cuts the
        wait before the next lookup short."""
        if (find := self.provider.find) is None:
            return None
        for delay in self.find_delays:
            with contextlib.suppress(TimeoutError):
                wait = max(0.0, min(delay, end - time.monotonic()))
                await asyncio.wait_for(batch.woken.wait(), wait)
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
            self._looked(batch)
            batch.woken.clear()
        return None

    async def _adopt(self, batch: _Batch, handle: str) -> bool:
        """Whether the batch known by its staged reference `handle` was found, its id then on
        its rows (it is known here by its identity already, so a call that reads the id joins
        this task) before it is polled by it; when not, it is done with: not found, its calls
        go as normal calls; an account error fails them, its rows left to recover."""
        assert batch.find_until is not None
        try:
            batch_id = await self._find(batch, handle.removeprefix(STAGED), batch.find_until)
        except ProviderError as exc:
            log.error("batch %s could not be looked for, for the account: %s", handle, exc)
            await self._account_error(exc)
            self._answer(batch, dict.fromkeys(batch.waiting, exc))
            return False
        journal = self.journal
        if batch_id is None:
            log.warning(
                "batch %s may have been made and was not found; its calls go as normal calls, "
                "which may pay for them twice",
                handle,
            )
            if journal is not None:
                await self._journalled(
                    "settle batch",
                    lambda: journal.settle_batch(handle, {}, rest=_MAY_HAVE_BEEN_MADE),
                )
            self._answer(batch, {})
            return False
        log.warning("batch %s found as %s", handle, batch_id)
        batch.name = batch_id
        if journal is not None:  # before it is polled, so a run that dies meanwhile reads it
            await self._journalled("batch adopted", lambda: journal.adopted(handle, batch_id))
        return True

    async def _check(self, batch: _Batch, now: datetime) -> bool:
        """Look at the batch once: True when it is done with, settled or failed on the account
        (its calls answered); False while it runs (cancelled at the deadline) or could not be
        read."""
        batch_id = batch.name
        assert batch_id is not None
        replies: dict[str, Answered] = {}
        try:
            status = await self._bounded(self.provider.status(batch_id))
            if status not in ("ended", "gone"):
                if now >= batch.deadline and not batch.cancelled:
                    await self._bounded(self.provider.cancel(batch_id))
                    batch.cancelled = True
                    log.info("batch %s cancelled at the deadline", batch_id)
                return False
            if status == "ended":
                replies = await self._bounded(self.provider.results(batch_id))
        except Exception as exc:
            if not (isinstance(exc, ProviderError) and exc.account):
                log.exception("batch %s could not be read; trying again", batch_id)
                return False
            # Not read again by this task: its calls fail on the account, and the batch is left
            # open on its rows for a later run to account for. Seen here, once, whether or not
            # a call waits on it (a dead run's batch): the provider is failed for its account.
            log.error("batch %s could not be read for the account: %s", batch_id, exc)
            await self._account_error(exc)
            self._answer(batch, dict.fromkeys(batch.waiting, exc))
            return True
        if status == "gone":  # deleted, or another workspace's: it never answers
            log.warning("batch %s is gone from its provider", batch_id)
        if (journal := self.journal) is not None:  # accounted for once, before its calls
            # What the results named no answer for ends here; a batch gone answers nothing.
            rest = {"ended": f"provider: {NOT_ANSWERED}", "gone": f"provider: {_GONE}"}[status]
            outcomes = {key: _outcome(reply, self.debug) for key, reply in replies.items()}
            await self._journalled(
                "settle batch", lambda: journal.settle_batch(batch_id, outcomes, rest=rest)
            )
        self._answer(batch, replies)
        return True

    async def _bounded[T](self, operation: Coroutine[None, None, T]) -> T:
        """A provider operation, failing (as the provider's own errors do) past `operation`."""
        return await asyncio.wait_for(operation, self.operation)


async def journalled[T](
    what: str, write: Callable[[], Awaitable[T]], delays: tuple[float, ...] = JOURNAL_DELAYS
) -> T:
    """A journal write made once the provider has answered, tried again after each of `delays`
    while the database is briefly away or the write lost a conflict, so the answer, which the
    caller keeps, is not lost with it."""
    for delay in (*delays, None):
        try:
            return await write()
        except Exception as exc:
            if delay is None or not retryable(exc):
                raise
            log.warning("journal: %s failed, trying again in %ss: %s", what, delay, exc)
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")
