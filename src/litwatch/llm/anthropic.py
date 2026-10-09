"""Claude models on Anthropic's own API."""

from __future__ import annotations

from typing import Any

import anthropic
from anthropic import AsyncAnthropic, transform_schema

from litwatch.config import RoleConfig
from litwatch.llm.prices import anthropic_cost
from litwatch.llm.types import ProviderError, Reply, Request, Stop, Usage

# Non-streaming requests stay under the SDK's timeout at this ceiling.
DEFAULT_MAX_TOKENS = 16000


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


def reply_of(message: Any, role: RoleConfig) -> Reply:
    u = message.usage
    cache_read = u.cache_read_input_tokens or 0
    cache_write = u.cache_creation_input_tokens or 0
    usage = Usage(
        model=role.model,
        via="anthropic",
        effort=role.effort,
        prompt_tokens=u.input_tokens + cache_read + cache_write,
        completion_tokens=u.output_tokens,
        cost=anthropic_cost(role.model, u.input_tokens, cache_read, cache_write, u.output_tokens),
        generation_id=message.id,
        provider="anthropic",
        service_tier=getattr(u, "service_tier", None),
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
        self.client = client or AsyncAnthropic(api_key=api_key)

    def routing(self, role: RoleConfig) -> tuple[str, ...]:
        return ()

    async def complete(self, role: RoleConfig, request: Request) -> Reply:
        try:
            message = await self.client.messages.create(**messages_request(role, request))
        except anthropic.APITimeoutError as exc:
            raise ProviderError(type(exc).__name__, timeout=True) from exc
        except (
            anthropic.InternalServerError,
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
        ) as exc:
            raise ProviderError(type(exc).__name__, transient=True) from exc
        except anthropic.APIError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
        return reply_of(message, role)
