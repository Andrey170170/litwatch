"""Roles to providers, with retries, a timeout, the call journal and reuse."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from litwatch.config import Models, RoleConfig, secret
from litwatch.llm.types import Provider, ProviderError, Reply, Request, Stop, Usage

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
    `timeout` or `provider`; the message carries the detail."""

    def __init__(self, kind: str, detail: str | None = None) -> None:
        self.kind, self.detail = kind, detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


class Journal(Protocol):
    """Where calls are recorded and found again. `store.calls.CallJournal` implements it."""

    async def cached(self, key: str) -> dict[str, Any] | None: ...
    async def start(self, role: str, model: str, key: str, paper_id: str | None) -> int: ...
    async def record(self, call_id: int, usage: Usage) -> None: ...
    async def accept(self, call_id: int, output: dict[str, Any]) -> None: ...
    async def reject(self, call_id: int, error: str) -> None: ...


@dataclass(frozen=True)
class Answer[T: BaseModel]:
    """A validated reply and what it cost."""

    value: T
    usage: Usage
    model: str  # `provider:model@effort`, for the record of who said it


class Gateway:
    """`ask(role, request)` is the whole interface the pipeline uses."""

    def __init__(
        self,
        models: Models,
        providers: dict[str, Provider],
        journal: Journal | None = None,
        *,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
    ) -> None:
        self.models, self.providers, self.journal = models, providers, journal
        self.retry_delays = retry_delays

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
        return hashlib.sha256(json.dumps(material, ensure_ascii=True).encode()).hexdigest()

    async def complete(self, role_name: str, request: Request) -> Reply:
        """One reply from the role's model, after retries. Not journalled; `ask` is."""
        role = self.models.role(role_name)
        provider = self._provider(role)
        for delay in (*self.retry_delays, None):
            try:
                async with asyncio.timeout(role.timeout_seconds):
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

    async def ask[T: BaseModel](
        self, role_name: str, request: Request, *, paper_id: str | None = None
    ) -> Answer[T]:
        """A validated answer, reusing an identical earlier call. Raises `CallError`.

        The reply is recorded before it is validated, so a rejected one still has its charge;
        only validated outputs are reusable.
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
        try:
            reply = await self.complete(role_name, request)
            reply.usage.source_call_id = call_id
            if journal:
                await journal.record(call_id, reply.usage)
            log.info("inference role=%s usage=%s", role_name, reply.usage.model_dump_json())
            if reply.stop == Stop.REFUSED:
                log.warning("role=%s paper=%s refused: %s", role_name, paper_id, reply.refusal)
                raise CallError("refused", reply.refusal)
            if reply.stop == Stop.TRUNCATED:
                raise CallError("truncated")
            try:
                value = request.output.model_validate(_without_nul(json.loads(reply.text)))
            except json.JSONDecodeError as exc:
                raise CallError("invalid", "not JSON") from exc
            except ValidationError as exc:
                raise CallError("invalid", f"{exc.error_count()} validation errors") from exc
        except CallError as exc:
            # No cost is invented for transport failures. A refusal keeps its category, so
            # `error LIKE 'refused%'` finds them all.
            if journal:
                await journal.reject(call_id, str(exc).replace("\x00", ""))
            raise
        if journal:
            await journal.accept(call_id, value.model_dump(mode="json"))
        return Answer(value, reply.usage, role.label)


def build(models: Models, journal: Journal | None = None) -> Gateway:
    """A gateway over the providers that have credentials in the environment."""
    from litwatch.llm.anthropic import AnthropicProvider  # noqa: PLC0415
    from litwatch.llm.openrouter import OpenRouterProvider  # noqa: PLC0415

    providers: dict[str, Provider] = {}
    if key := secret("ANTHROPIC_API_KEY"):
        providers["anthropic"] = AnthropicProvider(key)
    if key := secret("OPENROUTER_API_KEY"):
        providers["openrouter"] = OpenRouterProvider(key)
    return Gateway(models, providers, journal)
