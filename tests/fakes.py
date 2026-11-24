"""Stand-ins for the provider SDK clients and for a whole provider."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import openai

from litwatch.config import RoleConfig
from litwatch.intake.http import SourceError
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


def openai_body(
    text="{}",
    status="completed",
    *,
    model="gpt-6.2-sol-2026-11-01",
    tier="default",
    incomplete=None,
    refusal=None,
    error=None,
    tokens_in=4000,
    read=3000,
    write=0,
    output=200,
):
    """A Responses API response as the API sends it (a batch's output line carries this body),
    from `model`, the id as the API names it."""
    content = [{"type": "output_text", "text": text, "annotations": []}]
    if refusal is not None:
        content = [{"type": "refusal", "refusal": refusal}]
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1.0,
        "model": model,
        "service_tier": tier,
        "status": status,
        "error": error,
        "incomplete_details": {"reason": incomplete} if incomplete else None,
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": content,
            },
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": tokens_in,
            "input_tokens_details": {"cached_tokens": read, "cache_write_tokens": write},
            "output_tokens": output,
            "output_tokens_details": {"reasoning_tokens": 150},
            "total_tokens": tokens_in + output,
        },
    }


def openai_response(*args, **kwargs):
    """The SDK's `Response` for `openai_body`, as `responses.create` returns it."""
    from openai.types.responses import Response  # noqa: PLC0415

    return Response.model_validate(openai_body(*args, **kwargs))


def openai_answer(body):
    """A batch's answer to a request: a good one, from the model the request named, as named."""
    return openai_body('{"yes": true, "why": "it says so"}', model=body["model"], tier=None)


class OpenAIBatches:
    """The SDK's `files` and `batches` as the Batch API serves them. A batch's requests are
    answered once the test ends it (`end`), each by `respond(body)`: a response body (status
    200), or a `(status_code, error)` pair for a request the API failed, the status None for
    one it never ran (expired). Each of `creating`, in turn, is how a creation fails: whether
    the batch is made, and the error raised then (`"hang"`: it never returns). The list is the
    SDK's own, its pages served `page` at a time, newest first, through a mock transport:
    while `unlisted` it cannot be read, while `listed` is an HTTP status it answers with it, and
    while `hanging` it never answers; the next `misses` listings miss the newest batch.
    `listings` counts them; `read` lists the files whose content was read; `uploaded_then` runs
    once a file is uploaded."""

    def __init__(self, respond=openai_answer, *, page: int = 100):
        self.respond, self.page, self.misses, self.unlisted = respond, page, 0, False
        self.listed: int | None = None
        self.hanging = False
        self.uploaded_then: Any = None
        self.creating: list[tuple[bool, Any]] = []
        self.listings = 0
        self._hiding: str | None = None
        self.uploaded: dict[str, list[dict[str, Any]]] = {}
        self.made: dict[str, SimpleNamespace] = {}
        self.contents: dict[str, str] = {}
        self.read: list[str] = []
        sdk = openai.AsyncOpenAI(
            api_key="k",
            base_url="https://batches.test/v1",
            max_retries=0,
            http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self._serve)),
        )
        self.files = SimpleNamespace(create=self._upload, content=self._content)
        self.batches = SimpleNamespace(
            create=self._create, list=sdk.batches.list, retrieve=self._retrieve, cancel=self._cancel
        )
        self.responses: Any = None  # normal calls, for a test that makes them (`SdkClient`)

    async def _upload(self, *, file, purpose):
        assert purpose == "batch"
        name, data, _ = file
        file_id = f"file-{len(self.uploaded) + 1}"
        self.uploaded[file_id] = [json.loads(line) for line in data.decode().split("\n")]
        if self.uploaded_then is not None:
            self.uploaded_then()
        return SimpleNamespace(id=file_id)

    async def _create(self, *, input_file_id, endpoint, completion_window):
        made, error = self.creating.pop(0) if self.creating else (True, None)
        if made:
            (model,) = {line["body"]["model"] for line in self.uploaded[input_file_id]}
            batch_id = f"batch_{len(self.made) + 1}"
            self.made[batch_id] = SimpleNamespace(
                id=batch_id,
                status="in_progress",
                endpoint=endpoint,
                model=model,
                input_file_id=input_file_id,
                output_file_id=None,
                error_file_id=None,
                errors=None,
            )
        if error == "hang":
            await asyncio.Event().wait()
        if error is not None:
            raise error
        return self.made[batch_id]

    async def _serve(self, request: httpx2.Request) -> httpx2.Response:
        """`GET /batches`, as the API pages it."""
        assert request.method == "GET" and request.url.path == "/v1/batches"
        if self.hanging:
            await asyncio.Event().wait()
        if self.unlisted:
            raise httpx2.ConnectError("unlisted", request=request)
        if self.listed is not None:
            error = {"message": "no", "type": "invalid_request_error", "code": None}
            return httpx2.Response(self.listed, json={"error": error})
        after, limit = request.url.params.get("after"), int(request.url.params["limit"])
        newest = list(reversed(self.made.values()))
        if after is None:
            self.listings += 1
            self._hiding = newest[0].id if self.misses and newest else None
            self.misses = max(self.misses - 1, 0)
        newest = [b for b in newest if b.id != self._hiding]
        start = 0 if after is None else [b.id for b in newest].index(after) + 1
        data = newest[start : start + min(limit, self.page)]
        body = {
            "object": "list",
            "data": [vars(b) | {"object": "batch", "completion_window": "24h"} for b in data],
            "has_more": start + len(data) < len(newest),
        }
        return httpx2.Response(200, json=body)

    async def _retrieve(self, batch_id):
        if batch_id not in self.made:
            raise status_error(openai, 404)
        return self.made[batch_id]

    async def _cancel(self, batch_id):
        self.made[batch_id].status = "cancelling"

    async def _content(self, file_id):
        self.read.append(file_id)
        return SimpleNamespace(text=self.contents[file_id])

    def end(self, batch_id: str, *, failed: bool = False) -> None:
        """The batch completes with its answers; `failed`, its input refused with none."""
        batch = self.made[batch_id]
        if failed:
            batch.status = "failed"
            batch.errors = {"data": [{"code": "token_limit_exceeded"}]}
            return
        out, errors = [], []
        for request in self.uploaded[batch.input_file_id]:
            answer = self.respond(request["body"])
            line = {"id": "batch_req", "custom_id": request["custom_id"], "error": None}
            if isinstance(answer, tuple):
                code, error = answer
                if code is None:
                    line |= {"response": None, "error": error}
                else:
                    line["response"] = {"status_code": code, "body": {"error": error}}
                errors.append(line)
            else:
                line["response"] = {"status_code": 200, "request_id": "req", "body": answer}
                out.append(line)
        for kind, lines in (("output", out), ("error", errors)):
            if lines:
                file_id = f"file-{kind}-{batch_id}"
                self.contents[file_id] = (
                    "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n"
                )
                setattr(batch, f"{kind}_file_id", file_id)
        batch.status = "completed"


class SdkClient:
    """Every SDK surface at once; `replies` are returned in order, exceptions raised."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.sent: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.responses = SimpleNamespace(create=self._create)

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

    `respond(role, request)` returns the JSON text of the reply, a `Reply` or a
    `ProviderError` (raised, and in a batch given as the request's error), or raises.
    """

    def __init__(self, name: str, respond) -> None:
        self.name, self.respond = name, respond
        self.calls: list[tuple[RoleConfig, Request]] = []

    def routing(self, role: RoleConfig) -> tuple[str, ...]:
        return role.routing

    async def complete(self, role: RoleConfig, request: Request) -> Reply:
        self.calls.append((role, request))
        answer = self.respond(role, request)
        if isinstance(answer, ProviderError):
            raise answer
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

    find = None  # finding a batch is the OpenAI fake's (`OpenAIBatches`)

    def __init__(self, name: str, respond, *, hold: bool = False, one_model: bool = False) -> None:
        super().__init__(name, respond)
        self.hold, self.one_model = hold, one_model
        self.unanswered = lambda request: False
        self.batches: dict[str, dict[str, Any]] = {}
        self.batched: list[tuple[RoleConfig, Request]] = []
        self.cancelled: list[str] = []

    @property
    def submitted(self) -> list[list[str]]:
        """Each batch's keys, in the order sent."""
        return [[key for key, _, _ in b["requests"]] for b in self.batches.values()]

    async def submit(self, requests: list[tuple[str, RoleConfig, Request]], staged=None) -> str:
        if staged is not None:
            await staged(None)
        batch_id = f"msgbatch_{len(self.batches) + 1}"
        replies = {}
        for key, role, request in requests:
            if self.unanswered(request):
                continue
            self.batched.append((role, request))
            answer = self.respond(role, request)
            if not isinstance(answer, Reply | ProviderError):
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
        return self.batches[batch_id]["status"] if batch_id in self.batches else "gone"

    async def cancel(self, batch_id: str) -> None:
        self.cancelled.append(batch_id)
        if self.batches[batch_id]["status"] == "in_progress":
            self.batches[batch_id]["status"] = "canceling"

    async def results(self, batch_id: str) -> dict[str, Reply | ProviderError | None]:
        b = self.batches[batch_id]
        assert b["status"] == "ended", "results are read only once a batch has ended"
        return {key: b["replies"].get(key) for key, _, _ in b["requests"]}


__all__ = ["BatchingProvider", "ProviderError", "ScriptedProvider", "SdkClient"]


def unavailable(*args, **kwargs):
    raise SourceError("upstream unavailable")


# The batcher's timings in tests.
FAST: dict[str, Any] = {
    "quiet": 0.02,
    "poll": 0.01,
    "find_seconds": 1.0,
    "find_delays": (0.01, 0.01, 0.01),
}


def status_error(sdk, code: int, body: dict | None = None) -> Exception:
    """The SDK's error for an HTTP status, as it raises one; `body` is the response's `error`."""
    http = sys.modules[type(sdk.DefaultAsyncHttpxClient()).__mro__[1].__module__.split(".")[0]]
    request = http.Request("POST", "https://api.example.org")
    client = sdk.AsyncAnthropic(api_key="k") if sdk is anthropic else sdk.AsyncOpenAI(api_key="k")
    response = http.Response(code, request=request)
    body = None if body is None else {"error": body}
    return client._make_status_error(f"status {code}", body=body, response=response)
