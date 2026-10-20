"""Every other lab's models, through OpenRouter.

Each request names the providers allowed to serve it. A listed model is served only by its
providers (`only`), tried in order, so one provider's outage falls through to the next.
`require_parameters` skips endpoints that do not support the schema or reasoning
parameters; that check alone did not keep out endpoints that accept `reasoning` and return
no reasoning tokens, which is why the lists exist.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2  # the HTTP client this SDK is built on
import openai
from openai import AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema

from litwatch.config import RoleConfig
from litwatch.llm.types import (
    ACCOUNT_STATUSES,
    Choice,
    DecisionRequest,
    Predicate,
    ProviderError,
    Reply,
    Request,
    Stop,
    Usage,
)

BASE_URL = "https://openrouter.ai/api/v1"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"

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


def decisions_request(
    role: RoleConfig, request: DecisionRequest, routing: tuple[str, ...]
) -> dict[str, Any]:
    """The Decisions endpoint's call: `noul` for a yes/no question, `choice` for a choice."""
    questions: dict[str, Any] = {}
    for q in request.questions:
        match q:
            case Predicate():
                questions[q.name] = {"type": "noul", "instructions": q.instructions}
            case Choice():
                questions[q.name] = {
                    "type": "choice",
                    "instructions": q.instructions,
                    "criteria": dict(q.options),
                }
    body: dict[str, Any] = {"model": role.model, "state": request.content, "questions": questions}
    if routing:
        body["provider"] = {"order": list(routing), "only": list(routing)}
    return body


def _mapping(value: Any) -> dict[str, Any]:
    """A reply's object, or an empty one when it is malformed, so validation rejects it."""
    return value if isinstance(value, dict) else {}


def decision_reply(raw: Any, request: DecisionRequest, role: RoleConfig) -> Reply:
    """The answers as the JSON a chat model gives for the same request. An answer that is
    missing or of the wrong type is left out, so the gateway rejects the reply as invalid."""
    sent = json.dumps(raw, default=str)
    raw = raw if isinstance(raw, dict) else {}
    answers = _mapping(raw.get("answers"))
    out: dict[str, Any] = {}
    for q in request.questions:
        a = _mapping(answers.get(q.name))
        if isinstance(q, Predicate) and a.get("type") == "noul":
            out[q.name] = a.get("noul")
        elif isinstance(q, Choice) and a.get("type") == "choice":
            option = a.get("choice")
            probabilities = _mapping(a.get("probabilities"))
            # A choice that is not a label is left for validation to reject.
            known = isinstance(option, str)
            probability = probabilities.get(option, a.get("confidence")) if known else None
            out[q.name] = {"option": option, "probability": probability}
    values = _mapping(raw.get("usage"))
    usage = Usage(
        model=role.model,
        via="openrouter",
        prompt_tokens=values.get("input_tokens") or 0,
        completion_tokens=values.get("output_tokens") or 0,
        cost=values.get("cost"),
        generation_id=raw.get("id"),
        provider=raw.get("provider"),
    )
    return Reply(json.dumps(out), Stop.COMPLETE, usage, raw=sent)


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
    if said := getattr(choice.message, "refusal", None):
        said = "\n\n".join(filter(None, (text, said)))
        return Reply(text, Stop.REFUSED, usage, refusal="unspecified", raw=said)
    finish = getattr(choice, "finish_reason", None)
    if finish == "content_filter":
        return Reply(text, Stop.REFUSED, usage, refusal="content_filter")
    return Reply(text, Stop.TRUNCATED if finish == "length" else Stop.COMPLETE, usage)


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, api_key: str | None = None, client: Any = None) -> None:
        # The gateway owns retries; the SDK's own would multiply them.
        self.client = client or AsyncOpenAI(base_url=BASE_URL, api_key=api_key, max_retries=0)

    def routing(self, role: RoleConfig) -> tuple[str, ...]:
        return role.routing or DEFAULT_ROUTING.get(role.model, ())

    async def complete(self, role: RoleConfig, request: Request) -> Reply:
        body = chat_request(role, request, self.routing(role))
        completion = await self._call(self.client.chat.completions.create(**body))
        return reply_of(completion, role)

    async def decide(self, role: RoleConfig, request: DecisionRequest) -> Reply:
        body = decisions_request(role, request, self.routing(role))
        raw = await self._call(self.client.post(DECISIONS_URL, body=body, cast_to=object))
        return decision_reply(raw, request, role)

    async def _call(self, pending: Any) -> Any:
        """The SDK's failures as provider errors."""
        try:
            return await pending
        except openai.APITimeoutError as exc:
            if isinstance(exc.__cause__, httpx2.ConnectTimeout):
                # The connection never opened, so nothing was sent: worth another attempt.
                raise ProviderError("APITimeoutError while connecting", transient=True) from exc
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
            account = getattr(exc, "status_code", None) in ACCOUNT_STATUSES
            raise ProviderError(f"{type(exc).__name__}: {exc}", account=account) from exc
