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


def decisions_reply(answers: dict[str, Any], cost: float = 0.00005) -> dict[str, Any]:
    return {
        "id": "gen-dec-1",
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "answers": answers,
        "usage": {"input_tokens": 500, "output_tokens": 0, "cost": cost},
    }


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

    async def post(self, url: str, *, body: dict[str, Any], cast_to: Any) -> Any:
        """OpenRouter's Decisions endpoint, through the OpenAI SDK's raw `post`."""
        return await self._create(url=url, **body)

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


class BatchingProvider(ScriptedProvider):
    """A scripted provider with Message Batches (`llm.batches.BatchProvider`). A batch's
    replies are computed when it is sent, at half of `ScriptedProvider`'s price, and given once
    it ends: at once, unless `hold` is set, when the test ends it (`end`). A cancelled batch
    answers only the keys `end` names; `unanswered(request)` picks requests it never answers.
    Normal calls are `calls`; batched ones `batched`."""

    def __init__(self, name: str, respond, *, hold: bool = False) -> None:
        super().__init__(name, respond)
        self.hold = hold
        self.unanswered = lambda request: False
        self.batches: dict[str, dict[str, Any]] = {}
        self.batched: list[tuple[RoleConfig, Request]] = []
        self.cancelled: list[str] = []

    @property
    def submitted(self) -> list[list[str]]:
        """Each batch's keys, in the order sent."""
        return [[key for key, _, _ in b["requests"]] for b in self.batches.values()]

    async def submit(self, requests: list[tuple[str, RoleConfig, Request]]) -> str:
        batch_id = f"msgbatch_{len(self.batches) + 1}"
        replies = {}
        for key, role, request in requests:
            if self.unanswered(request):
                continue
            self.batched.append((role, request))
            answer = self.respond(role, request)
            if not isinstance(answer, Reply):
                usage = Usage(model=role.model, via=self.name, cost=0.0005, service_tier="batch")
                answer = Reply(answer, Stop.COMPLETE, usage)
            replies[key] = answer
        status = "in_progress" if self.hold else "ended"
        self.batches[batch_id] = {"requests": requests, "replies": replies, "status": status}
        return batch_id

    def end(self, batch_id: str, answered: set[str] | None = None) -> None:
        """End the batch; a cancelled one keeps the replies of `answered` only."""
        b = self.batches[batch_id]
        if batch_id in self.cancelled:
            b["replies"] = {k: r for k, r in b["replies"].items() if k in (answered or set())}
        b["status"] = "ended"

    async def status(self, batch_id: str) -> str:
        return self.batches[batch_id]["status"]

    async def cancel(self, batch_id: str) -> None:
        self.cancelled.append(batch_id)
        if self.batches[batch_id]["status"] == "in_progress":
            self.batches[batch_id]["status"] = "canceling"

    async def results(self, batch_id: str) -> dict[str, Reply | None]:
        b = self.batches[batch_id]
        assert b["status"] == "ended", "results are read only once a batch has ended"
        return {key: b["replies"].get(key) for key, _, _ in b["requests"]}


__all__ = ["BatchingProvider", "ProviderError", "ScriptedProvider", "SdkClient"]
