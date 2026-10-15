"""Roles to providers, with retries, a timeout, the call journal and reuse."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from pydantic import BaseModel, ValidationError

from litwatch.config import Models, RoleConfig, secret
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
    `timeout` or `provider`; the message carries the detail. `usage` is the reply's, when one
    came back and was billed."""

    def __init__(self, kind: str, detail: str | None = None, usage: Usage | None = None) -> None:
        # Provider text can carry a NUL, which Postgres text cannot hold; the message is
        # stored as a paper's last error.
        detail = detail.replace("\x00", "") if detail else detail
        self.kind, self.detail, self.usage = kind, detail, usage
        super().__init__(f"{kind}: {detail}" if detail else kind)

    @property
    def unusable(self) -> bool:
        """The model answered, and the answer could not be used: asking again is the same
        question to the same model. A timeout, a provider error or missing credentials say
        nothing about the paper."""
        return self.kind in ("invalid", "refused", "truncated")


class Journal(Protocol):
    """Where calls are recorded and found again. `store.calls.CallJournal` implements it."""

    async def cached(self, key: str) -> dict[str, Any] | None: ...
    async def start(self, role: str, model: str, key: str, paper_id: str | None) -> int: ...
    async def record(self, call_id: int, usage: Usage) -> None: ...
    async def accept(self, call_id: int, output: dict[str, Any]) -> None: ...
    async def reject(self, call_id: int, error: str, reply: str | None = None) -> None: ...


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
    invalid one's error carries what the validator said."""

    def __init__(
        self,
        models: Models,
        providers: dict[str, Provider],
        journal: Journal | None = None,
        *,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        debug: bool = False,
    ) -> None:
        self.models, self.providers, self.journal = models, providers, journal
        self.retry_delays, self.debug = retry_delays, debug

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
        request: Request,
        send: Callable[[Provider, RoleConfig], Awaitable[Reply]] | None = None,
    ) -> Reply:
        """One reply from the role's model, after retries. Not journalled; `ask` is. `send`
        replaces the chat call (a Decisions endpoint)."""
        role = self.models.role(role_name)
        provider = self._provider(role)
        for delay in (*self.retry_delays, None):
            try:
                async with asyncio.timeout(role.timeout_seconds):
                    if send:
                        return await send(provider, role)
                    return await provider.complete(role, request)
            except TimeoutError as exc:
                raise CallError("timeout", f"{role.timeout_seconds:g}s") from exc
            except ProviderError as exc:
                if exc.timeout:
                    raise CallError("timeout", str(exc)) from exc
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
    ) -> Answer[T]:
        """A validated answer, reusing an identical earlier call. Raises `CallError`.

        The reply is recorded before it is validated, so a rejected one still has its charge;
        only validated outputs are reusable. The reply's text is kept only in debug mode.
        """
        role = self.models.role(role_name)
        key = self.request_key(role, request)
        journal = self.journal
        if journal and (hit := await journal.cached(key)):
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
        call_id = await journal.start(role_name, role.model, key, paper_id) if journal else None
        reply = None
        try:
            reply = await self.complete(role_name, request, send)
            reply.usage.source_call_id = call_id
            if journal:
                await journal.record(call_id, reply.usage)
            log.info("inference role=%s usage=%s", role_name, reply.usage.model_dump_json())
            if reply.stop == Stop.REFUSED:
                log.warning("role=%s paper=%s refused: %s", role_name, paper_id, reply.refusal)
                raise CallError("refused", reply.refusal, reply.usage)
            if reply.stop == Stop.TRUNCATED:
                raise CallError("truncated", usage=reply.usage)
            try:
                value = request.output.model_validate(_without_nul(json.loads(reply.text)))
            except json.JSONDecodeError as exc:
                raise CallError("invalid", "not JSON", reply.usage) from exc
            except ValidationError as exc:
                # Where it failed, not what was said: out of debug mode the reply is not kept.
                errors = [(".".join(map(str, e["loc"])) or "reply", e["msg"]) for e in exc.errors()]
                if self.debug:
                    said = "; ".join(f"{where}: {msg}" for where, msg in errors)
                else:
                    said = ", ".join(sorted({where for where, _ in errors}))
                detail = f"{exc.error_count()} validation errors: {said}"
                raise CallError("invalid", detail, reply.usage) from exc
        except CallError as exc:
            # No cost is invented for transport failures. A refusal keeps its category, so
            # `error LIKE 'refused%'` finds them all.
            if journal:
                kept = _without_nul(reply.text) if self.debug and reply else None
                await journal.reject(call_id, str(exc), kept)
            raise
        if journal:
            await journal.accept(call_id, value.model_dump(mode="json"))
        return Answer(value, reply.usage, role.label)


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
