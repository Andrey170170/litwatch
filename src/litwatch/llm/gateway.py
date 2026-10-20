"""Roles to providers, with retries, a timeout, the call journal and reuse, and in a batching
job Message Batches (`llm.batches`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from pydantic import BaseModel, ValidationError

from litwatch.config import Models, RoleConfig, secret
from litwatch.llm.batches import MAX_REQUESTS, Answered, Batcher, BatchProvider
from litwatch.llm.decisions import Decision, chat_request, decision_of
from litwatch.llm.types import (
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
    """Where calls are recorded and found again. `store.calls.CallJournal` implements it."""

    async def cached(self, key: str) -> dict[str, Any] | None: ...
    async def start(
        self, role: str, model: str, key: str, paper_id: str | None, via: str | None = None
    ) -> int: ...
    async def record(self, call_id: int, usage: Usage) -> None: ...
    async def accept(self, call_id: int, output: dict[str, Any]) -> None: ...
    async def reject(self, call_id: int, error: str, reply: str | None = None) -> None: ...
    async def refused(self, key: str, paper_id: str | None) -> bool: ...
    async def in_batch(self, key: str) -> dict[str, Any] | None: ...
    async def open_batches(self) -> list[dict[str, Any]]: ...
    async def batched(self, call_ids: list[int], batch_id: str, provider: str) -> None: ...
    async def answered(
        self, batch_id: str, key: str, usage: Usage | None, error: str | None = None
    ) -> None: ...
    async def account_error(self, provider: str, error: str) -> None: ...


@dataclass(frozen=True)
class Answer[T: BaseModel]:
    """A validated reply and what it cost."""

    value: T
    usage: Usage
    model: str  # `provider:model@effort`, for the record of who said it


class DecisionProvider(Provider, Protocol):
    """A provider with a Decisions endpoint. It answers in the JSON a chat model would give for
    the same request (`decisions.chat_request`)."""

    async def decide(self, role: RoleConfig, request: DecisionRequest) -> Reply: ...


class Gateway:
    """`ask(role, request)` and `decide(role, request)` are the whole interface the pipeline
    uses. In `debug` mode a reply that could not be used is journalled with the call, and an
    invalid one's error carries what the validator said. After `batch_until`, the calls on a
    provider that batches go in its batches; the caller does not know."""

    def __init__(
        self,
        models: Models,
        providers: dict[str, Provider],
        journal: Journal | None = None,
        *,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        debug: bool = False,
        normal_calls: int = NORMAL_CALLS,
        batch_options: dict[str, Any] | None = None,
    ) -> None:
        self.models, self.providers, self.journal = models, providers, journal
        self.retry_delays, self.debug = retry_delays, debug
        self.batch_options = batch_options or {}  # the `Batcher`'s timings and clock (tests)
        self.batchers: dict[str, Batcher] = {}
        self._gates = {name: asyncio.Semaphore(normal_calls) for name in providers}

    def batch_until(self, deadline: datetime, *, drain: timedelta | None = None) -> None:
        """Send each call on a provider that batches in its batches until `deadline` (aware),
        and as a normal call after it; batches open then are waited on for `drain` (by default
        the batcher's)."""
        options = self.batch_options | ({} if drain is None else {"drain": drain})
        self.batchers = {
            name: Batcher(provider, self.journal, deadline, **options)
            for name, provider in self.providers.items()
            if isinstance(provider, BatchProvider)
        }

    async def recover(self) -> None:
        """Watch the batches a dead run left open, so each is accounted for in full whether or
        not its calls are asked again (past the deadline, cancelled first)."""
        if not self.journal or not self.batchers:
            return
        for row in await self.journal.open_batches():
            batcher = self.batchers.get(row["provider"])  # the batch's, whatever roles use now
            if batcher is not None:
                batcher.watch(row["batch_id"])

    @property
    def batching(self) -> bool:
        return any(b.open() for b in self.batchers.values())

    def batching_until(self) -> datetime | None:
        """The deadline while batching, else None."""
        return next((b.deadline for b in self.batchers.values() if b.open()), None)

    def width(self, limit: int) -> int:
        """A step's calls in flight: all of them while batching, so a step is one batch (normal
        calls stay bounded per provider); else `limit`."""
        return MAX_REQUESTS if self.batching else limit

    async def settle(self) -> None:
        """Wait until the open batches, a dead run's included, are read or given up on."""
        for batcher in self.batchers.values():
            await batcher.settle()

    async def close(self) -> None:
        """Stop batching; calls still waiting on a batch go as normal calls."""
        for batcher in self.batchers.values():
            await batcher.close()

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
        send: Callable[[Provider, RoleConfig], Awaitable[Reply]] | None = None,
    ) -> Reply:
        """One reply from `role`, a model of the role's list, after retries. Not journalled;
        `ask` is. `send` replaces the chat call (a Decisions endpoint)."""
        provider = self._provider(role)
        for delay in (*self.retry_delays, None):
            try:
                async with self._gates[role.provider], asyncio.timeout(role.timeout_seconds):
                    if send:
                        return await send(provider, role)
                    return await provider.complete(role, request)
            except TimeoutError as exc:
                raise CallError("timeout", f"{role.timeout_seconds:g}s") from exc
            except ProviderError as exc:
                if exc.timeout:
                    raise CallError("timeout", str(exc)) from exc
                if exc.account:
                    raise CallError("account", str(exc)) from exc
                if delay is None or not exc.transient:
                    raise CallError("provider", str(exc)) from exc
                log.warning("role=%s %s, trying again in %ss", role_name, exc, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def decide(
        self, role_name: str, request: DecisionRequest, *, paper_id: str | None = None
    ) -> Answer[Decision]:
        """A validated decision: every question answered, every probability in [0, 1], every
        choice one of its options. Raises `CallError` as `ask` does."""
        role = self.models.role(role_name)
        chat = chat_request(request)
        send = None
        if role.decisions:

            async def send(provider: Provider, role: RoleConfig) -> Reply:
                # Configuration allows a Decisions endpoint only on OpenRouter.
                return await cast(DecisionProvider, provider).decide(role, request)

        answer = await self.ask(role_name, chat, paper_id=paper_id, send=send)
        return Answer(decision_of(answer.value, request), answer.usage, answer.model)

    async def ask[T: BaseModel](
        self,
        role_name: str,
        request: Request,
        *,
        paper_id: str | None = None,
        send: Callable[[Provider, RoleConfig], Awaitable[Reply]] | None = None,
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
        by the journal, and none has a validated answer to it (one an evaluation got, say, which
        `ask` reuses): a refusal remembered from an earlier run holds only while the question
        and the list are unchanged. A model without credentials now has not: asked, it is a
        call with no answer."""
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
        send: Callable[[Provider, RoleConfig], Awaitable[Reply]] | None = None,
    ) -> Answer[T]:
        """One model of the role's list, asked and journalled under the role's name."""
        key = self.request_key(role, request)
        journal = self.journal
        call_id, reply = await self._batched(role_name, role, key, request, paper_id, send)
        try:
            if isinstance(reply, ProviderError):  # the account kept the batch from answering
                raise CallError("account", str(reply))
            if reply is None:
                reply = await self.complete(role_name, role, request, send)
            reply.usage.source_call_id = call_id
            if journal:
                await journal.record(call_id, reply.usage)
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
                # Where it failed; in debug mode what the validator said there too.
                errors = [(".".join(map(str, e["loc"])) or "reply", e["msg"]) for e in exc.errors()]
                if self.debug:
                    said = "; ".join(f"{where}: {msg}" for where, msg in errors)
                else:
                    said = ", ".join(sorted({where for where, _ in errors}))
                detail = f"{exc.error_count()} validation errors: {said}"
                raise CallError("invalid", detail, reply.usage) from exc
        except CallError as exc:
            exc.model = role.label
            # No cost is invented for transport failures. A refusal keeps its category, so
            # `error LIKE 'refused%'` finds them all.
            if journal:
                answered = self.debug and isinstance(reply, Reply)
                kept = _without_nul(reply.raw or reply.text) if answered else None
                await journal.reject(call_id, str(exc), kept)
                if exc.kind == "account":  # a normal call's or a batch's, alike
                    await journal.account_error(role.provider, str(exc))
            raise
        if journal:
            await journal.accept(call_id, value.model_dump(mode="json"))
        return Answer(value, reply.usage, role.label)

    async def _batched(
        self,
        role_name: str,
        role: RoleConfig,
        key: str,
        request: Request,
        paper_id: str | None,
        send: Callable[[Provider, RoleConfig], Awaitable[Reply]] | None,
    ) -> tuple[int | None, Answered]:
        """The call's journal row, and its reply or account error when a batch gave one (None:
        send it as a normal call). A key a dead run left in an open batch waits on that batch,
        on its row, past the deadline too (the batch is cancelled then); one the batch did not
        answer is sent as a normal call. A normal call has a row of its own when the batch it
        was given up on may still answer it on its row. A fallback is batched as the role's
        model is."""
        journal = self.journal

        async def row_of_its_own() -> int:
            assert journal is not None
            return await journal.start(role_name, role.model, key, paper_id, role.provider)

        batcher = self.batchers.get(role.provider) if send is None else None
        if batcher and journal and (row := await journal.in_batch(key)):
            reply = await batcher.attach(row["batch_id"], key, row["id"])
            if isinstance(reply, Reply):  # settles that row, given up on before or not
                if reply.usage.reused:  # shared with a call on that row: a row of its own
                    return await row_of_its_own(), reply
                return row["id"], _of_role(reply, role)
            if row["id"] in batcher.unsettled:  # the batch may still answer it on that row
                return await row_of_its_own(), reply
            if isinstance(reply, ProviderError):
                return row["id"], reply
            await journal.reject(row["id"], "provider: the batch did not answer it")
            return await row_of_its_own(), None
        call_id = await row_of_its_own() if journal else None
        if batcher is None or not batcher.open():
            return call_id, None
        reply = await batcher.ask(key, role, request, call_id)
        if not isinstance(reply, Reply) and journal and call_id in batcher.unsettled:
            return await row_of_its_own(), reply
        return call_id, _of_role(reply, role) if isinstance(reply, Reply) else reply


def _of_role(reply: Reply, role: RoleConfig) -> Reply:
    """A batch's reply knows its model, not the effort it was asked at."""
    reply.usage.effort = role.effort
    return reply


def build(models: Models, journal: Journal | None = None, *, debug: bool = False) -> Gateway:
    """A gateway over the providers that have credentials in the environment."""
    from litwatch.llm.anthropic import AnthropicProvider  # noqa: PLC0415
    from litwatch.llm.openrouter import OpenRouterProvider  # noqa: PLC0415

    providers: dict[str, Provider] = {}
    if key := secret("ANTHROPIC_API_KEY"):
        providers["anthropic"] = AnthropicProvider(key)
    if key := secret("OPENROUTER_API_KEY"):
        providers["openrouter"] = OpenRouterProvider(key)
    return Gateway(models, providers, journal, debug=debug)
