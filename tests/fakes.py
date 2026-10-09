"""Stand-ins for the provider SDK clients and for a whole provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from litwatch.config import RoleConfig
from litwatch.llm import ProviderError, Reply, Request, Stop, Usage


def anthropic_message(
    text="{}", stop_reason="end_turn", fresh=400, read=3000, write=0, output=200, category=None
):
    return SimpleNamespace(
        id="msg_1",
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if category else None,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=fresh,
            cache_read_input_tokens=read,
            cache_creation_input_tokens=write,
            output_tokens=output,
        ),
    )


def openrouter_completion(text="{}", finish_reason="stop", refusal=None, cost=0.0021):
    return SimpleNamespace(
        id="gen-1",
        provider="Meta",
        service_tier=None,
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=text, refusal=refusal),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=900,
            completion_tokens=350,
            cost=cost,
            prompt_tokens_details={"cached_tokens": 0},
            completion_tokens_details={"reasoning_tokens": 300},
        ),
    )


class SdkClient:
    """Both SDK surfaces at once; `replies` are returned in order, exceptions raised."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.sent: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **request: Any) -> Any:
        self.sent.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class ScriptedProvider:
    """A provider whose answer is computed from the request by `respond`.

    `respond(role, request)` returns the JSON text of the reply, a `Reply`, or raises.
    """

    def __init__(self, name: str, respond) -> None:
        self.name, self.respond = name, respond
        self.calls: list[tuple[RoleConfig, Request]] = []

    def routing(self, role: RoleConfig) -> tuple[str, ...]:
        return role.routing

    async def complete(self, role: RoleConfig, request: Request) -> Reply:
        self.calls.append((role, request))
        answer = self.respond(role, request)
        if isinstance(answer, Reply):
            return answer
        usage = Usage(model=role.model, via=self.name, effort=role.effort, cost=0.001)
        return Reply(answer, Stop.COMPLETE, usage)


__all__ = ["ProviderError", "ScriptedProvider", "SdkClient"]
