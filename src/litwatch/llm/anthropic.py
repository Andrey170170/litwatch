"""Claude models on Anthropic's own API."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2  # the HTTP client this SDK is built on
from anthropic import AsyncAnthropic, transform_schema

from litwatch.config import RoleConfig
from litwatch.llm.prices import anthropic_cost
from litwatch.llm.types import ACCOUNT_STATUSES, ProviderError, Reply, Request, Stop, Usage

# Non-streaming requests stay under the SDK's timeout at this ceiling.
DEFAULT_MAX_TOKENS = 16000
# A batch's request errors that are the account's (401, 402, 403), by their error type.
BATCH_ACCOUNT = frozenset({"authentication_error", "billing_error", "permission_error"})


def messages_request(role: RoleConfig, request: Request) -> dict[str, Any]:
    """The Messages API call for a request."""
    output_config: dict[str, Any] = {
        "format": {
            "type": "json_schema",
            "schema": transform_schema(request.output.model_json_schema()),
        }
    }
    if role.effort:
        output_config["effort"] = role.effort
    return {
        "model": role.model,
        "max_tokens": request.max_output_tokens or role.max_output_tokens or DEFAULT_MAX_TOKENS,
        # The system prompt repeats across a run's papers; a short one is silently not cached.
        "system": [
            {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": request.user}],
        "thinking": {"type": "adaptive"},
        "output_config": output_config,
    }


def reply_of(message: Any, role: RoleConfig | None, *, batch: bool = False) -> Reply:
    """The reply of a message, priced at the batch rate for an answer from a batch. Without the
    role (an answer of a batch no call awaits) the model is the message's and the effort unknown."""
    model = role.model if role else message.model
    u = message.usage
    cache_read = u.cache_read_input_tokens or 0
    cache_write = u.cache_creation_input_tokens or 0
    usage = Usage(
        model=model,
        via="anthropic",
        effort=role.effort if role else None,
        prompt_tokens=u.input_tokens + cache_read + cache_write,
        completion_tokens=u.output_tokens,
        cost=anthropic_cost(
            model, u.input_tokens, cache_read, cache_write, u.output_tokens, batch=batch
        ),
        generation_id=message.id,
        provider="anthropic",
        service_tier="batch" if batch else getattr(u, "service_tier", None),
        cached_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
    text = "".join(b.text for b in message.content if b.type == "text")
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) or "unspecified"
        return Reply(text, Stop.REFUSED, usage, refusal=category)
    stop = Stop.TRUNCATED if message.stop_reason == "max_tokens" else Stop.COMPLETE
    return Reply(text, stop, usage)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, client: Any = None) -> None:
        # The gateway owns retries; the SDK's own would multiply them.
        self.client = client or AsyncAnthropic(api_key=api_key, max_retries=0)

    def routing(self, role: RoleConfig) -> tuple[str, ...]:
        return ()

    async def complete(self, role: RoleConfig, request: Request) -> Reply:
        try:
            message = await self.client.messages.create(**messages_request(role, request))
        except anthropic.APITimeoutError as exc:
            if isinstance(exc.__cause__, httpx2.ConnectTimeout):
                # The connection never opened, so nothing was sent: worth another attempt.
                raise ProviderError("APITimeoutError while connecting", transient=True) from exc
            raise ProviderError(type(exc).__name__, timeout=True) from exc
        except (
            anthropic.InternalServerError,
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
        ) as exc:
            raise ProviderError(type(exc).__name__, transient=True) from exc
        except anthropic.APIError as exc:
            account = getattr(exc, "status_code", None) in ACCOUNT_STATUSES
            raise ProviderError(f"{type(exc).__name__}: {exc}", account=account) from exc
        return reply_of(message, role)

    # Message Batches (models.md, batches): `custom_id` is the gateway's request key.

    async def submit(self, requests: list[tuple[str, RoleConfig, Request]]) -> str:
        batch = await self.client.messages.batches.create(
            requests=[
                {"custom_id": key, "params": messages_request(role, request)}
                for key, role, request in requests
            ]
        )
        return batch.id

    async def status(self, batch_id: str) -> str:
        """`in_progress`, `canceling` or `ended`; `gone` when the API no longer has it (deleted,
        or the key is another workspace's)."""
        try:
            return (await self.client.messages.batches.retrieve(batch_id)).processing_status
        except anthropic.NotFoundError:
            return "gone"

    async def cancel(self, batch_id: str) -> None:
        await self.client.messages.batches.cancel(batch_id)

    async def results(self, batch_id: str) -> dict[str, Reply | ProviderError | None]:
        """Each request's reply, by its key; an account error for one the account kept the
        batch from answering, never asked again; None for one the batch did not answer
        otherwise (errored, cancelled, expired), which is asked again as a normal call."""
        found: dict[str, Reply | ProviderError | None] = {}
        async for line in await self.client.messages.batches.results(batch_id):
            result = line.result
            if result.type == "succeeded":
                found[line.custom_id] = reply_of(result.message, None, batch=True)
            elif result.type == "errored" and (kind := result.error.error.type) in BATCH_ACCOUNT:
                found[line.custom_id] = ProviderError(f"{kind} in batch {batch_id}", account=True)
            else:
                found[line.custom_id] = None
        return found
