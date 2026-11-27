"""The Model gate (docs/specs/architecture/a2-work-gate.md, "The Model gate"): one per process,
built by its owner (`serve`, a CLI command, a test), holding the providers, each provider's limit
on normal calls, and the batchers, kept per provider and deadline (`llm.batches`), so the calls
of every item the process runs share them. Roles go to providers with retries, a timeout, reuse
and the call journal.

A run reaches the gate through its own door (`ModelGate.on`, a `Gateway`), which journals its
calls on the run and on the work item each serves (`gates.context.current_work`). The ceiling a
call reserves against and the deadline its batch must be answered by come from the context the
Worker sets for its item (`llm.context`), not from the call site. A call whose request a batch
not yet settled holds waits on that batch, a dead run's included, as its deadline says, unless a
live claim other than its item's sent it and this gate does not follow it
(`store.calls.CallJournal.in_batch`; `Gateway._batched`). Each batch has one owner record in the
gate (`llm.batches._Batch`), with its own deadline; each caller keeps its own wait.
`recover`, once as the process starts, takes over the batches no live claim will read, so each is
accounted for on its calls' own rows, their run and item kept; one a live claim sent is taken over
once that claim's lease lapses unrenewed (a process killed with its lease still live)."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, Self, cast

from pydantic import BaseModel, ValidationError

from litwatch.config import Models, RoleConfig, secret
from litwatch.core.budget import CallStart
from litwatch.gates.context import current_work
from litwatch.gates.database import attributed_call, outside_unit
from litwatch.gates.work import retryable
from litwatch.llm import ceiling as ceilings
from litwatch.llm import context
from litwatch.llm.batches import (
    JOURNAL_DELAYS,
    MAX_REQUESTS,
    Answered,
    Batcher,
    BatchJournal,
    BatchProvider,
    journalled,
)
from litwatch.llm.decisions import Decision, chat_request, decision_of
from litwatch.llm.types import (
    DEFAULT_MAX_TOKENS,
    DecisionRequest,
    Provider,
    ProviderError,
    Reply,
    Request,
    Stop,
    Usage,
)

log = logging.getLogger(__name__)

# A provider that is briefly away is tried again after each of these; a timeout is not, since
# every attempt already had the whole timeout.
RETRY_DELAYS = (1.0, 5.0, 20.0)
# Bump when validation changes what counts as an acceptable reply.
VALIDATION_VERSION = 1
NORMAL_CALLS = 16  # normal calls in flight per provider, whatever the steps' own limits
# The key of the batcher that sends nothing and follows the batches a call without a deadline
# finds no one following: each under its own deadline (`Batcher.attach`), as at a deadline,
# cancelled at once and then drained.
NO_DEADLINE = datetime.min.replace(tzinfo=UTC)
# The least a recovery waits before looking again at a batch a live item's lease keeps.
RECHECK_SECONDS = 1.0


def _without_nul(value: Any) -> Any:
    """Model text can carry a NUL, which Postgres stores in neither text nor jsonb."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_without_nul(v) for v in value]
    if isinstance(value, dict):
        return {k: _without_nul(v) for k, v in value.items()}
    return value


class CallError(Exception):
    """A call produced no usable answer. `kind` is `truncated`, `refused`, `invalid`,
    `timeout`, `provider` or `account` (the provider's 401, 402 or 403, but for OpenRouter's
    403 about the request's content, a refusal: never retried); the
    message carries the detail. `usage` is the reply's, when one
    came back and was billed; `model` the model asked last (`provider:model@effort`). From
    `Gateway.ask`, `refused` means every model asked down the role's list refused."""

    def __init__(
        self,
        kind: str,
        detail: str | None = None,
        usage: Usage | None = None,
        model: str | None = None,
    ) -> None:
        # Provider text can carry a NUL, which Postgres text cannot hold; the message is
        # stored as a paper's last error.
        detail = detail.replace("\x00", "") if detail else detail
        self.kind, self.detail, self.usage, self.model = kind, detail, usage, model
        super().__init__(f"{kind}: {detail}" if detail else kind)

    @property
    def unusable(self) -> bool:
        """The model answered, and the answer could not be used: asking again is the same
        question to the same model. A timeout, a provider or account error or missing
        credentials say nothing about the paper."""
        return self.kind in ("invalid", "refused", "truncated")


class Journal(Protocol):
    """Where a run's calls are recorded and found again (`store.calls.CallJournal`). Without
    `observes` (an evaluation's), its calls neither batch nor wait on a batch."""

    run_id: int | None
    observes: bool

    async def cached(self, key: str) -> dict[str, Any] | None: ...
    async def start(self, call: CallStart) -> int: ...
    async def record(self, call_id: int, usage: Usage) -> None: ...
    async def accept(self, call_id: int, output: dict[str, Any]) -> None: ...
    async def reject(
        self,
        call_id: int,
        error: str,
        reply: str | None = None,
        *,
        account: str | None = None,
        usage: Usage | None = None,
    ) -> None: ...
    async def refused(self, key: str, paper_id: str | None) -> bool: ...
    async def in_batch(
        self, key: str, work_id: int | None, owned: list[int]
    ) -> dict[str, Any] | None: ...


class GateJournal(BatchJournal, Protocol):
    """The gate's own journal: its batches' calls, and the batches left open."""

    async def open_batches(self, batches: list[int] | None = None) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Answer[T: BaseModel]:
    """A validated reply and what it cost."""

    value: T
    usage: Usage
    model: str  # `provider:model@effort`, for the record of who said it


class DecisionProvider(Provider, Protocol):
    """A provider with a Decisions endpoint. It answers in the JSON a chat model would give for
    the same request (`decisions.chat_request`)."""

    async def decide(
        self, role: RoleConfig, request: DecisionRequest, *, max_price: dict | None = None
    ) -> Reply: ...


class ModelGate:
    """The process's one Model gate over `providers`, serving `models` unless a door names its
    own (`on`). `journal` is the gate's own (`store.calls.CallJournal` of no run): its batches'
    calls are opened and settled on it, whichever run they are of. Closing it stops its
    batchers: calls still waiting on a batch go as normal calls, and a batch still open is left
    on its rows for the next process to account for."""

    def __init__(
        self,
        models: Models,
        providers: Mapping[str, Provider],
        journal: GateJournal | None = None,
        *,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        debug: bool = False,
        normal_calls: int = NORMAL_CALLS,
        batch_options: dict[str, Any] | None = None,
    ) -> None:
        self.models, self.providers, self.journal = models, providers, journal
        self.retry_delays, self.debug = retry_delays, debug
        self.batch_options = batch_options or {}  # the `Batcher`'s timings and clock (tests)
        self.now: Callable[[], datetime] = self.batch_options.get("now", lambda: datetime.now(UTC))
        self.journal_delays: tuple[float, ...] = self.batch_options.get(
            "journal_delays", JOURNAL_DELAYS
        )
        self.limits = {name: asyncio.Semaphore(normal_calls) for name in providers}
        self._batchers: dict[tuple[str, datetime], Batcher] = {}
        self._recovering: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def on(
        self,
        journal: Journal | None = None,
        *,
        models: Models | None = None,
        retry_delays: tuple[float, ...] | None = None,
    ) -> Gateway:
        """A door to the gate for one run's calls, journalled on `journal` (none: not at all),
        serving `models` (by default the gate's), with `retry_delays` its own when given (an
        evaluation's)."""
        return Gateway(
            self,
            journal,
            models or self.models,
            self.retry_delays if retry_delays is None else retry_delays,
            self.debug,
        )

    def batcher(self, provider: str, deadline: datetime) -> Batcher | None:
        """The batcher of `provider`'s calls with `deadline` (`NO_DEADLINE`: of the calls
        without one), made at its first call; None for a provider that does not batch. One
        whose drain is over and that holds nothing is let go as another is made."""
        found = self.providers.get(provider)
        if not isinstance(found, BatchProvider):
            return None
        if (provider, deadline) not in self._batchers:
            self._batchers = {k: b for k, b in self._batchers.items() if not b.idle()}
            self._batchers[(provider, deadline)] = Batcher(
                found, self.journal, deadline, debug=self.debug, **self.batch_options
            )
        return self._batchers[(provider, deadline)]

    def owner(self, batch: int) -> Batcher | None:
        """The batcher that follows the batch of identity `batch`, if any: each batch has one
        owner record in a process, kept by that batcher."""
        return next((b for b in self._batchers.values() if b.holds(batch)), None)

    def owned(self) -> list[int]:
        """The identities of the batches this gate follows."""
        return [i for b in self._batchers.values() for i in b.followed()]

    async def recover(self, deadline: datetime) -> int:
        """Take over the batches left open that no live claim will read (a dead process's, or
        one given up on), each followed until it ends or `deadline`'s drain is over (cancelled
        at the deadline), so each is accounted for in full, on its calls' rows, whether or not
        its calls are asked again: once, as the process starts. One a live claim sent then (its
        process killed with its lease still live, or a command still running) is looked at
        again, by its identity, as that lease lapses (`_reconsidered`). Returns how many it
        takes over now."""
        if self.journal is None:
            return 0
        rows = await self.journal.open_batches()
        taken = self._take_over(rows, deadline)
        if blocked := [r["batch"] for r in rows if r["live_until"] is not None]:
            self._recovering = asyncio.create_task(self._reconsidered(blocked, deadline))
            self._recovering.add_done_callback(_logged)
        return taken

    def _take_over(self, rows: list[dict[str, Any]], deadline: datetime) -> int:
        """Follow each batch of `rows` that no live claim sent and no batcher here follows, by
        its provider whatever roles use now (one without credentials now is left), under the
        name it goes by now."""
        taken = 0
        for row in rows:
            if row["live_until"] is not None or self.owner(row["batch"]) is not None:
                continue
            if (batcher := self.batcher(row["provider"], deadline)) is not None:
                batcher.watch(row["batch"], row["batch_id"])
                taken += 1
        return taken

    async def _reconsidered(self, blocked: list[int], deadline: datetime) -> None:
        """The batches a live claim sent as the process started, looked at again, by identity,
        as the earliest of their leases lapses: each taken over once no live claim holds it (its
        lease lapsed unrenewed, the item taken back under a new claim, or ended), and let go
        once it is settled or followed here (its item, run again here, attached to it). A
        command that keeps its lease keeps its batch. A read that fails is tried again."""
        assert self.journal is not None
        while blocked:
            try:
                rows = await self.journal.open_batches(blocked)
            except Exception as exc:
                if not retryable(exc):
                    raise
                log.warning("model gate: looking at the batches left again failed: %s", exc)
                await asyncio.sleep(RECHECK_SECONDS)
                continue
            self._take_over(rows, deadline)
            held = [
                r for r in rows if r["live_until"] is not None and self.owner(r["batch"]) is None
            ]
            blocked = [r["batch"] for r in held]
            if held:
                lapse = min(r["live_until"] for r in held) - datetime.now(UTC)
                await asyncio.sleep(max(RECHECK_SECONDS, lapse.total_seconds() + 0.5))

    async def settle(self, deadline: datetime) -> None:
        """Wait until the batches of calls with `deadline` are read or given up on."""
        for (_, until), batcher in list(self._batchers.items()):
            if until == deadline:
                await batcher.settle()

    async def close(self) -> None:
        """Stop recovering and batching; calls still waiting on a batch go as normal calls."""
        if self._recovering is not None:
            self._recovering.cancel()
            await asyncio.gather(self._recovering, return_exceptions=True)
        for batcher in list(self._batchers.values()):
            await batcher.close()
        self._batchers = {}


class Gateway:
    """One run's door to the Model gate. `ask(role, request)` and `decide(role, request)` are
    the whole interface the pipeline uses. An invalid reply's error carries what the validator
    said; in `debug` mode a reply that could not be used is also journalled with the call. While
    the work context has a deadline ahead (`llm.context`), the calls on a provider that batches
    go in the gate's batches for that deadline; the caller does not know."""

    def __init__(
        self,
        gate: ModelGate,
        journal: Journal | None,
        models: Models,
        retry_delays: tuple[float, ...],
        debug: bool,
    ) -> None:
        self.gate, self.journal, self.models = gate, journal, models
        self.retry_delays, self.debug = retry_delays, debug

    @property
    def providers(self) -> Mapping[str, Provider]:
        return self.gate.providers

    @property
    def ceiling(self) -> ceilings.Ceiling | None:
        """The ceiling this task's calls reserve against (`llm.context`), if any."""
        return context.current().ceiling

    def batching_until(self) -> datetime | None:
        """The deadline this task's calls batch until, while it is ahead and some provider
        batches, else None. An evaluation's calls never batch."""
        deadline = context.current().deadline
        if deadline is None or self.journal is None or not self.journal.observes:
            return None
        batches = any(isinstance(p, BatchProvider) for p in self.providers.values())
        return deadline if batches and self.gate.now() < deadline else None

    @property
    def batching(self) -> bool:
        return self.batching_until() is not None

    def width(self, limit: int) -> int:
        """A step's calls in flight: all of them while batching, so a step is one batch (normal
        calls stay bounded per provider); else `limit`."""
        return MAX_REQUESTS if self.batching else limit

    async def settle(self) -> None:
        """Wait until the batches of this task's deadline, a dead run's it took over included,
        are read or given up on."""
        if (deadline := context.current().deadline) is not None:
            await self.gate.settle(deadline)

    def _provider(self, role: RoleConfig) -> Provider:
        if role.provider not in self.providers:
            raise CallError("provider", f"no credentials for {role.provider}")
        return self.providers[role.provider]

    def request_key(self, role: RoleConfig, request: Request) -> str:
        """Identical means the same resolved provider, model, effort and routing as well as
        the same request, so two roles never share a reply and a model change is a new call."""
        material = {
            "validation_version": VALIDATION_VERSION,
            "provider": role.provider,
            "model": role.model,
            "effort": role.effort,
            "routing": list(self._provider(role).routing(role)),
            "max_output_tokens": role.max_output_tokens,
            "request": request.key_material(),
        }
        if role.decisions:  # another endpoint, another answer; absent keeps older keys
            material["decisions"] = True
        return hashlib.sha256(json.dumps(material, ensure_ascii=True).encode()).hexdigest()

    async def complete(
        self,
        role_name: str,
        role: RoleConfig,
        request: Request,
        send: Callable[..., Awaitable[Reply]] | None = None,
    ) -> Reply:
        """One reply from `role`, a model of the role's list, after retries. Not journalled;
        `ask` is. `send` replaces the chat call (a Decisions endpoint). Under a `ceiling`, each
        attempt first reserves the most it can cost (`llm.ceiling`)."""
        outside_unit(f"a model call for role {role_name!r}")
        attributed_call(f"a model call for role {role_name!r}")
        return await self._complete(role_name, role, request, send)

    async def _complete(
        self,
        role_name: str,
        role: RoleConfig,
        request: Request,
        send: Callable[..., Awaitable[Reply]] | None = None,
        opened: Callable[[float | None], Awaitable[None]] | None = None,
        failed: Callable[[ProviderError, Usage | None], Awaitable[None]] | None = None,
    ) -> Reply:
        """`complete`, for a caller that checked the nesting itself (`ask`), which keeps a
        journal row per send: `opened` before each attempt is sent, once the ceiling admits
        it, with what it reserved, and `failed` told of each attempt that failed and is tried
        again, with what it was charged (`_failure_usage`), as the last one's `CallError`
        carries it."""
        provider = self._provider(role)
        bound = self.ceiling  # the work context's, for every attempt of this call
        role, limits = self._limits(bound, role, request)
        for delay in (*self.retry_delays, None):
            reserved = await self._reserve(bound, role, request)
            cost: float | None = None  # what the attempt was charged, when known
            # Whether it went out: an attempt stopped before (cancelled waiting for the
            # provider's limit, or its row not opened) was charged nothing.
            sent = False
            try:
                async with self.gate.limits[role.provider]:
                    if opened is not None:
                        await opened(reserved)
                    sent = True
                    async with asyncio.timeout(role.timeout_seconds):
                        if send:
                            reply = await send(provider, role, **limits)
                        else:
                            reply = await provider.complete(role, request, **limits)
                cost = reply.usage.cost
                return reply
            except TimeoutError as exc:
                raise CallError("timeout", f"{role.timeout_seconds:g}s") from exc
            except ProviderError as exc:
                # A timeout, a lost connection, a 5xx or an unknown charge keeps its reservation.
                usage = _failure_usage(exc, role)
                cost = usage.cost if usage is not None else None
                if exc.timeout:
                    raise CallError("timeout", str(exc)) from exc
                if exc.account:
                    raise CallError("account", str(exc), usage) from exc
                if delay is None or not exc.transient:
                    raise CallError("provider", str(exc), usage) from exc
                if failed is not None:
                    await failed(exc, usage)
                log.warning("role=%s %s, trying again in %ss", role_name, exc, delay)
            finally:
                if reserved is not None and bound is not None:
                    await bound.settle(reserved, cost if sent else 0.0)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _limits(
        under: ceilings.Ceiling | None, role: RoleConfig, request: Request
    ) -> tuple[RoleConfig, dict[str, Any]]:
        """Under a ceiling, the role with the output cap its route's bound sets when it has none,
        and the prices sent with the call (`max_price`); else as it is."""
        if under is None:
            return role, {}
        bound = under.bound(role, ceilings.input_tokens(request))
        if bound is None:
            return role, {}
        if bound.max_output and not (request.max_output_tokens or role.max_output_tokens):
            role = role.model_copy(update={"max_output_tokens": bound.max_output})
        return role, {"max_price": bound.max_price} if bound.max_price else {}

    @staticmethod
    async def _reserve(
        under: ceilings.Ceiling | None, role: RoleConfig, request: Request
    ) -> float | None:
        """Reserve the attempt's most against the ceiling, if there is one; raises `CallError`
        `unpriced` for a route without a price bound, `ceiling` when it does not fit."""
        if under is None:
            return None
        tokens_in = ceilings.input_tokens(request)
        bound = under.bound(role, tokens_in)
        if bound is None:
            raise CallError("unpriced", f"{role.label} has no price bound to run under a ceiling")
        most = under.most(bound, tokens_in, _max_output(role, request))
        try:
            await under.reserve(most)
        except ceilings.CeilingReachedError as exc:
            raise CallError("ceiling", f"${most:.4f} does not fit in what is left") from exc
        return most

    async def decide(
        self, role_name: str, request: DecisionRequest, *, paper_id: str | None = None
    ) -> Answer[Decision]:
        """A validated decision: every question answered, every probability in [0, 1], every
        choice one of its options. Raises `CallError` as `ask` does."""
        role = self.models.role(role_name)
        chat = chat_request(request)
        send = None
        if role.decisions:

            async def send(provider: Provider, role: RoleConfig, **limits: Any) -> Reply:
                # Configuration allows a Decisions endpoint only on OpenRouter.
                return await cast(DecisionProvider, provider).decide(role, request, **limits)

        answer = await self.ask(role_name, chat, paper_id=paper_id, send=send)
        return Answer(decision_of(answer.value, request), answer.usage, answer.model)

    async def ask[T: BaseModel](
        self,
        role_name: str,
        request: Request,
        *,
        paper_id: str | None = None,
        send: Callable[..., Awaitable[Reply]] | None = None,
        start: int = 0,
    ) -> Answer[T]:
        """A validated answer from the role's list: its model, then its fallbacks (models.md,
        roles). An answer any of them gave to the identical question is reused. Otherwise the
        model at `start` is asked (the step's unusable answers in a row, D4; past the end, the
        last): a refusal goes to the next at once, past the end to the top, each journalled under
        the role with its model, and when every model in the list refused, the last refusal is
        raised. Any other failure is raised at once: an unusable answer moves down only with the
        next run's `start`, and a call that got no answer never does.

        The reply is recorded before it is validated, so a rejected one still has its charge;
        only validated outputs are reusable. The reply's text is kept only in debug mode.
        """
        # Before the journal is read: `decide`, a reused answer and a batched call come through
        # here too, and none of them runs inside a Database-gate unit.
        outside_unit(f"a model call for role {role_name!r}")
        attributed_call(f"a model call for role {role_name!r}")
        models = self.models.role(role_name).models
        for model in models:  # a model without credentials can have answered nothing
            if model.provider in self.providers and (answer := await self._reused(model, request)):
                return answer
        refusal: CallError | None = None
        start = min(start, len(models) - 1)
        for model in models[start:] + models[:start]:
            if refusal:
                log.info("role=%s paper=%s refused: asking %s", role_name, paper_id, model.label)
            try:
                return await self._ask(role_name, model, request, paper_id=paper_id, send=send)
            except CallError as exc:
                if exc.kind != "refused":
                    raise
                refusal = exc
        assert refusal is not None
        raise refusal

    async def refused_throughout(
        self, role_name: str, request: Request, *, paper_id: str | None = None
    ) -> bool:
        """Whether every model of the role's list, as it is now, refused this identical question,
        by the journal, and none has a validated answer to it (one an earlier run got, which
        `ask` reuses; an evaluation's calls are no production run's, so neither its answers nor
        its refusals count here): a refusal remembered from an earlier run holds only while the
        question and the list are unchanged. A model without credentials now has not: asked, it
        is a call with no answer."""
        outside_unit(f"the model journal read for role {role_name!r}")
        attributed_call(f"the model journal read for role {role_name!r}")
        if not self.journal:
            return False
        for model in self.models.role(role_name).models:
            if model.provider not in self.providers:
                return False
            key = self.request_key(model, request)
            if await self.journal.cached(key) or not await self.journal.refused(key, paper_id):
                return False
        return True

    async def _reused[T: BaseModel](self, role: RoleConfig, request: Request) -> Answer[T] | None:
        """The validated answer of an identical earlier call to `role`, at no cost."""
        if not self.journal or not (
            hit := await self.journal.cached(self.request_key(role, request))
        ):
            return None
        original = Usage.model_validate(hit["usage"])
        usage = original.model_copy(
            update={
                "cost": 0.0,
                "reused": True,
                "original_cost": original.cost,
                "source_call_id": hit["id"],
            }
        )
        return Answer(request.output.model_validate(hit["output"]), usage, role.label)

    async def _ask[T: BaseModel](
        self,
        role_name: str,
        role: RoleConfig,
        request: Request,
        *,
        paper_id: str | None = None,
        send: Callable[..., Awaitable[Reply]] | None = None,
    ) -> Answer[T]:
        """One model of the role's list, asked and journalled under the role's name."""
        key = self.request_key(role, request)
        journal = self.journal
        # A batch's reply comes on its row, settled by its batcher but for the output.
        call_id, reply, attached = await self._batched(
            role_name, role, key, request, paper_id, send
        )
        if attached and reply is None and (answer := await self._reused(role, request)):
            return answer  # its batch's answer, saved meanwhile (another call validated it)
        batched = isinstance(reply, Reply)
        charged: Usage | None = None  # what a send that failed was charged, settled with it

        async def opened(reserved: float | None) -> None:
            """Each normal send on a row of its own, with what it reserved."""
            nonlocal call_id
            call_id = await self._start(role_name, role, key, paper_id, reserved)

        async def failed(exc: ProviderError, usage: Usage | None) -> None:
            """A send that failed and is tried again, settled with its charge where known, and
            let go: the next send has a row of its own once admitted."""
            nonlocal call_id
            if journal and (failed_id := call_id) is not None:
                await self._journalled(
                    "reject", lambda: journal.reject(failed_id, f"provider: {exc}", usage=usage)
                )
            call_id = None

        try:
            if isinstance(reply, ProviderError):  # the account kept the batch from answering
                raise CallError("account", str(reply))
            if reply is None:
                try:
                    reply = await self._complete(role_name, role, request, send, opened, failed)
                except CallError as exc:
                    charged = exc.usage  # the last attempt's charge; a failure, not a reply
                    raise
            if call_id is not None:
                reply.usage.source_call_id = call_id
            if journal and (recorded := call_id) is not None and not batched:
                usage = reply.usage
                await self._journalled("record", lambda: journal.record(recorded, usage))
            log.info("inference role=%s usage=%s", role_name, reply.usage.model_dump_json())
            if reply.stop == Stop.REFUSED:
                log.warning(
                    "role=%s model=%s paper=%s refused: %s",
                    role_name,
                    role.label,
                    paper_id,
                    reply.refusal,
                )
                raise CallError("refused", reply.refusal, reply.usage)
            if reply.stop == Stop.TRUNCATED:
                raise CallError("truncated", usage=reply.usage)
            try:
                value = request.output.model_validate(_without_nul(json.loads(reply.text)))
            except json.JSONDecodeError as exc:
                raise CallError("invalid", "not JSON", reply.usage) from exc
            except ValidationError as exc:
                # Where it failed and what the validator said there; never the reply's text.
                errors = [(".".join(map(str, e["loc"])) or "reply", e["msg"]) for e in exc.errors()]
                said = "; ".join(f"{where}: {msg}" for where, msg in errors)
                detail = f"{exc.error_count()} validation errors: {said}"
                raise CallError("invalid", detail, reply.usage) from exc
        except CallError as exc:
            exc.model = role.label
            # No cost is invented for transport failures. A refusal keeps its category, so
            # `error LIKE 'refused%'` finds them all. A call never sent (the ceiling did not
            # admit it) or one its batch answered with an error has no row of its own to settle,
            # and a batch's refusal or truncation is settled on its row by its batcher.
            settled = batched and exc.kind in ("refused", "truncated")
            if journal and (rejected := call_id) is not None and not settled:
                answered = self.debug and isinstance(reply, Reply)
                kept = _without_nul(reply.raw or reply.text) if answered else None
                # A normal call's account error is observed with it; a batch's by its batcher.
                normal = exc.kind == "account" and not isinstance(reply, ProviderError)
                error = str(exc)
                await self._journalled(
                    "reject",
                    lambda: journal.reject(
                        rejected,
                        error,
                        kept,
                        account=role.provider if normal else None,
                        usage=charged,
                    ),
                )
            raise
        if journal and (accepted := call_id) is not None:
            output = value.model_dump(mode="json")
            await self._journalled("accept", lambda: journal.accept(accepted, output))
        return Answer(value, reply.usage, role.label)

    async def _journalled(self, what: str, write: Callable[[], Awaitable[None]]) -> None:
        """A journal write once the provider has answered, tried again while the database is
        briefly away, the answer kept here meanwhile (`llm.batches.journalled`). It runs to its
        end in a task of its own, outside the item's context (its units are the gate's
        bookkeeping): the item's cancellation (its lease lost, `serve` stopping), however many
        times it comes, waits for it, so a charge the provider made is not lost with the item,
        and is raised once it has ended."""
        task = asyncio.create_task(
            journalled(what, write, self.gate.journal_delays), context=contextvars.Context()
        )
        cancelled: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.cancelled():  # the write's own (the loop closing), not the item's
                    raise
                cancelled = cancelled or exc  # its message, why the item stopped, kept
            except Exception:
                break  # the write's failure, raised below
        if cancelled is not None:
            if (exc := task.exception()) is not None:
                log.error("journal: %s failed as its call was cancelled: %r", what, exc)
            raise cancelled
        task.result()

    def _call(
        self,
        role_name: str,
        role: RoleConfig,
        key: str,
        paper_id: str | None,
        reserved: float | None = None,
    ) -> CallStart:
        """A send's journal row, with its run and work item, configured model and effort."""
        assert self.journal is not None and self.journal.run_id is not None, "a run's journal"
        work = current_work()
        return CallStart(
            run_id=self.journal.run_id,
            work_id=work.item.id if work else None,
            operation=role_name,
            model=role.model,
            key=key,
            paper_id=paper_id,
            via=role.provider,
            effort=role.effort,
            reserved=reserved,
        )

    async def _start(
        self,
        role_name: str,
        role: RoleConfig,
        key: str,
        paper_id: str | None,
        reserved: float | None,
    ) -> int | None:
        """A send's journal row, before any reply."""
        if not self.journal:
            return None
        return await self.journal.start(self._call(role_name, role, key, paper_id, reserved))

    async def _batched(
        self,
        role_name: str,
        role: RoleConfig,
        key: str,
        request: Request,
        paper_id: str | None,
        send: Callable[..., Awaitable[Reply]] | None,
    ) -> tuple[int | None, Answered, bool]:
        """A batch's reply and the journal row it was answered on (its batcher settled the
        row's charge, and its refusal or truncation), or the account error a batch gave the
        call; (None, None) to send it as a normal call, on rows of its own; and whether it
        attached to a batch, whose answer may have been saved meanwhile. One lookup decides
        (`store.calls.CallJournal.in_batch`): a refusal or truncation a batch gave this item's
        attempt before is consumed, as that reply again; a key in a batch not yet settled that
        this gate follows, or no other live claim sent, waits on that batch, on its row, through
        the batcher that follows it, else this deadline's, ahead or past (`NO_DEADLINE`
        without one), as the call's deadline says (`Batcher.attach`): until it, or the batch's
        own if later, and its drain (a nightly started after its deadline has one past); a call
        with no deadline looks once, then goes as a normal call. Under a deadline ahead the call
        goes in that deadline's batches. A fallback is batched as the role's model is; an
        evaluation's call never is."""
        journal = self.journal
        if send is not None or journal is None or not journal.observes:
            return None, None, False
        if not isinstance(self.providers.get(role.provider), BatchProvider):
            return None, None, False  # no batch of it can hold the request
        deadline = context.current().deadline
        work = current_work()
        owned = self.gate.owned()
        if row := await journal.in_batch(key, work.item.id if work else None, owned):
            if row["error"] is not None:
                return row["id"], _of_role(_stopped(row), role), False
            batcher = self.gate.owner(row["batch"]) or self.gate.batcher(
                role.provider, deadline or NO_DEADLINE
            )
            if batcher is None:
                return None, None, False
            batch, name = row["batch"], row["batch_id"]
            reply = await batcher.attach(batch, name, key, row["id"], until=deadline)
            attached = True
        elif (until := self.batching_until()) and (
            batcher := self.gate.batcher(role.provider, until)
        ):
            start = self._call(role_name, role, key, paper_id)
            reply = await batcher.ask(key, role, request, start)
            attached = False
        else:
            return None, None, False
        if not isinstance(reply, Reply):
            return None, reply, attached
        return reply.usage.source_call_id, _of_role(reply, role), attached


def _stopped(row: dict[str, Any]) -> Reply:
    """The refusal or truncation a batch gave the call on `row`, given again at no cost, on
    that row."""
    original = Usage.model_validate(row["usage"])
    usage = original.model_copy(
        update={
            "cost": 0.0,
            "reused": True,
            "original_cost": original.cost,
            "source_call_id": row["id"],
        }
    )
    error: str = row["error"]
    if error.startswith("refused"):
        return Reply("", Stop.REFUSED, usage, refusal=error.partition(": ")[2] or None)
    return Reply("", Stop.TRUNCATED, usage)


def _logged(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("model gate: recovering stopped: %r", exc)


def _max_output(role: RoleConfig, request: Request) -> int:
    """The most output tokens a call can be billed: its cap, or Anthropic's default one (a route
    without a default is given its cap by `Gateway._limits`, or bills no output)."""
    return request.max_output_tokens or role.max_output_tokens or DEFAULT_MAX_TOKENS


def _of_role(reply: Reply, role: RoleConfig) -> Reply:
    """A batch's reply as the role asked it: its model as configured and its effort, which the
    reply does not know."""
    reply.usage.model, reply.usage.effort = role.model, role.effort
    return reply


def _failure_usage(exc: ProviderError, role: RoleConfig) -> Usage | None:
    """What a failed attempt was charged: the usage it carries; nothing, for one the provider
    rejected (a 4xx); None when unknown (a timeout, a lost connection, a 5xx)."""
    if exc.usage is not None:
        return exc.usage
    if exc.timeout or exc.unanswered:
        return None
    return Usage(model=role.model, via=role.provider, effort=role.effort, cost=0.0)


# Each provider's key, read with `secret` (the variable or its `_FILE`).
KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def build(models: Models, journal: GateJournal | None = None, *, debug: bool = False) -> ModelGate:
    """A Model gate over the providers that have credentials in the environment."""
    from litwatch.llm.anthropic import AnthropicProvider  # noqa: PLC0415
    from litwatch.llm.openai import OpenAIProvider  # noqa: PLC0415
    from litwatch.llm.openrouter import OpenRouterProvider  # noqa: PLC0415

    kinds = {
        "anthropic": AnthropicProvider,
        "openrouter": OpenRouterProvider,
        "openai": OpenAIProvider,
    }
    providers: dict[str, Provider] = {
        name: kinds[name](key) for name, variable in KEYS.items() if (key := secret(variable))
    }
    return ModelGate(models, providers, journal, debug=debug)
