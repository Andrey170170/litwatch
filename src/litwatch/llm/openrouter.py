"""Every other lab's models, through OpenRouter.

Each request names the providers allowed to serve it. A listed model is served only by its
providers (`only`), tried in order, so one provider's outage falls through to the next.
`require_parameters` skips endpoints that do not support the schema or reasoning
parameters; that check alone did not keep out endpoints that accept `reasoning` and return
no reasoning tokens, which is why the lists exist.
"""

from __future__ import annotations

from typing import Any

import openai
from openai import AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema

from litwatch.config import RoleConfig
from litwatch.llm.types import ProviderError, Reply, Request, Stop, Usage

BASE_URL = "https://openrouter.ai/api/v1"

# Chosen from the 0.x inference journal and a provider trial; see evals/2026-10-01-providers.md.
DEFAULT_ROUTING: dict[str, tuple[str, ...]] = {
    "openai/gpt-5.6-luna": ("openai/flex", "openai"),
    "openai/gpt-6-luna": ("openai/flex", "openai"),
    "z-ai/glm-5.3": ("baidu", "reka", "wafer"),
    "meta/muse-spark-1.3": ("meta",),
    "meta/muse-spark-1.3-contributor": ("meta",),
    "moonshotai/kimi-k3": ("wafer", "moonshotai", "fireworks"),
}


def chat_request(role: RoleConfig, request: Request, routing: tuple[str, ...]) -> dict[str, Any]:
    """The chat-completions call for a request."""
    provider: dict[str, Any] = {"require_parameters": True}
    if routing:
        provider |= {"order": list(routing), "only": list(routing)}
    # `usage.include` makes OpenRouter report the call's cost in the usage block.
    extra: dict[str, Any] = {"usage": {"include": True}, "provider": provider}
    if role.effort:
        extra["reasoning"] = {"effort": role.effort}
    out: dict[str, Any] = {
        "model": role.model,
        "messages": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": request.output.__name__,
                "strict": True,
                "schema": to_strict_json_schema(request.output),
            },
        },
        "extra_body": extra,
    }
    if limit := request.max_output_tokens or role.max_output_tokens:
        out["max_completion_tokens"] = limit
    return out


def _dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return value.model_dump() if hasattr(value, "model_dump") else vars(value)


def reply_of(completion: Any, role: RoleConfig) -> Reply:
    """Keep provider-reported billing and cache details, including missing values."""
    raw = _dict(completion)
    values = _dict(completion.usage)
    values = {**(values.get("model_extra") or {}), **values}
    prompt = _dict(values.get("prompt_tokens_details"))
    output = _dict(values.get("completion_tokens_details"))
    usage = Usage(
        model=role.model,
        via="openrouter",
        effort=role.effort,
        prompt_tokens=values.get("prompt_tokens") or 0,
        completion_tokens=values.get("completion_tokens") or 0,
        cost=values.get("cost"),
        generation_id=raw.get("id"),
        provider=raw.get("provider"),
        service_tier=raw.get("service_tier"),
        cached_tokens=prompt.get("cached_tokens"),
        cache_write_tokens=prompt.get("cache_write_tokens"),
        reasoning_tokens=output.get("reasoning_tokens"),
    )
    choices = getattr(completion, "choices", None)
    if not choices:
        raise ProviderError("the completion has no choices", transient=True)
    choice = choices[0]
    text = choice.message.content or ""
    if getattr(choice.message, "refusal", None):
        return Reply(text, Stop.REFUSED, usage, refusal="unspecified")
    finish = getattr(choice, "finish_reason", None)
    if finish == "content_filter":
        return Reply(text, Stop.REFUSED, usage, refusal="content_filter")
    return Reply(text, Stop.TRUNCATED if finish == "length" else Stop.COMPLETE, usage)


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, api_key: str | None = None, client: Any = None) -> None:
        self.client = client or AsyncOpenAI(base_url=BASE_URL, api_key=api_key)

    def routing(self, role: RoleConfig) -> tuple[str, ...]:
        return role.routing or DEFAULT_ROUTING.get(role.model, ())

    async def complete(self, role: RoleConfig, request: Request) -> Reply:
        body = chat_request(role, request, self.routing(role))
        try:
            completion = await self.client.chat.completions.create(**body)
        except openai.APITimeoutError as exc:
            raise ProviderError(type(exc).__name__, timeout=True) from exc
        # OpenRouter answers 404 when a model's only provider has no endpoint up, which the
        # SDK never retries.
        except (
            openai.NotFoundError,
            openai.InternalServerError,
            openai.RateLimitError,
            openai.APIConnectionError,
        ) as exc:
            raise ProviderError(type(exc).__name__, transient=True) from exc
        except openai.APIError as exc:
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
        return reply_of(completion, role)
