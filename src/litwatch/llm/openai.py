"""OpenAI's models on OpenAI's own API: the Responses API with structured output for normal
calls, and the Batch API for batched ones (`llm.batches`), each priced from `llm.prices`."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections.abc import Generator, Iterable
from typing import Any

import httpx2  # the HTTP client this SDK is built on
import openai
from openai import AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema

from litwatch.config import RoleConfig
from litwatch.llm.batches import Answered
from litwatch.llm.prices import openai_cost, openai_model
from litwatch.llm.types import ACCOUNT_STATUSES, ProviderError, Reply, Request, Stop, Usage

log = logging.getLogger(__name__)

# Explicit, so an `OPENAI_BASE_URL` in the environment never sends the calls elsewhere.
BASE_URL = "https://api.openai.com/v1"
ENDPOINT = "/v1/responses"
# A 429 that is the account's rather than the rate's: its credit, spend or usage limit used up
# (OpenAI's error codes guide). Asking again does not help.
BILLING = frozenset(
    {
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "insufficient_quota",
    }
)
# A request declined for its content, by its error's code (the SDK's `ResponseError` lists
# them): a refusal, which goes down the role's list, rather than a provider's failure.
DECLINED = frozenset({"invalid_prompt", "bio_policy", "misalignment_policy_violation"})
# The Batch API's statuses as the batcher reads them (`batches.BatchProvider.status`).
STATUSES = {
    "validating": "in_progress",
    "in_progress": "in_progress",
    "finalizing": "in_progress",
    "cancelling": "canceling",
    "completed": "ended",
    "expired": "ended",
    "cancelled": "ended",
    "failed": "ended",  # its input was refused: nothing is answered, all goes as normal calls
}


def _status(status: str) -> str:
    """A status this module does not know is read as still running: polled until it ends."""
    return STATUSES.get(status, "in_progress")


TAG = "litwatch_requests"  # a batch's metadata: the digest of its request keys (`_tag`)
LISTED = 100  # the newest batches looked through for one sent already


def responses_request(role: RoleConfig, request: Request) -> dict[str, Any]:
    """The Responses API call for a request. Only the system prompt is cached: it repeats across
    a run's papers, and in explicit mode the paper after its breakpoint is never written to the
    cache, which bills a write (OpenAI's prompt-caching guide). Nothing is stored."""
    body: dict[str, Any] = {
        "model": role.model,
        "input": [
            {
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": request.system,
                        "prompt_cache_breakpoint": {"mode": "explicit"},
                    }
                ],
            },
            {"role": "user", "content": request.user},
        ],
        "prompt_cache_options": {"mode": "explicit"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": request.output.__name__,
                "strict": True,
                "schema": to_strict_json_schema(request.output),
            }
        },
        "store": False,
    }
    if role.effort:
        body["reasoning"] = {"effort": role.effort}
    if limit := request.max_output_tokens or role.max_output_tokens:
        body["max_output_tokens"] = limit
    return body


def _declined(error: Any) -> str | None:
    """The refusal category of an error about the request's content, else None."""
    code = error.get("code") if isinstance(error, dict) else None
    return code if code in DECLINED else None


def _billing(error: Any) -> bool:
    """Whether a 429's error is the account's limit rather than the rate's."""
    if not isinstance(error, dict):
        return False
    return error.get("code") in BILLING or error.get("type") == "insufficient_quota"


def reply_of(body: dict[str, Any], role: RoleConfig | None, *, batch: bool = False) -> Reply:
    """The reply of a response, as the API sends it, priced at the batch rate for an answer
    from a batch. Without the role (an answer of a batch no call awaits) the model is the
    response's and the effort unknown. A response that failed is a provider error."""
    model = role.model if role else openai_model(body.get("model") or "")
    u = body.get("usage")
    cost = None
    cache_read = cache_write = reasoning = None
    if u:
        details = u.get("input_tokens_details") or {}
        cache_read = details.get("cached_tokens", 0)
        cache_write = details.get("cache_write_tokens", 0)
        reasoning = (u.get("output_tokens_details") or {}).get("reasoning_tokens")
        cost = openai_cost(
            model, u["input_tokens"], cache_read, cache_write, u["output_tokens"], batch=batch
        )
    usage = Usage(
        model=model,
        via="openai",
        effort=role.effort if role else None,
        prompt_tokens=u["input_tokens"] if u else 0,
        completion_tokens=u["output_tokens"] if u else 0,
        cost=cost,
        generation_id=body.get("id"),
        provider="openai",
        service_tier="batch" if batch else body.get("service_tier"),
        cached_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
    )
    status = body.get("status")
    if status == "failed":
        error = body.get("error") or {}
        transient = error.get("code") in ("server_error", "rate_limit_exceeded")
        raise ProviderError(f"response failed: {error}", transient=transient, cost=usage.cost)
    parts = [
        part
        for item in body.get("output") or []
        if item.get("type") == "message"
        for part in item.get("content") or []
    ]
    text = "".join(p.get("text") or "" for p in parts if p.get("type") == "output_text")
    if refusals := [p.get("refusal") or "" for p in parts if p.get("type") == "refusal"]:
        said = "\n\n".join(filter(None, (text, *refusals)))
        return Reply(text, Stop.REFUSED, usage, refusal="unspecified", raw=said)
    if status == "incomplete":
        reason = (body.get("incomplete_details") or {}).get("reason")
        if reason == "content_filter":
            return Reply(text, Stop.REFUSED, usage, refusal="content_filter")
        return Reply(text, Stop.TRUNCATED, usage)
    if status != "completed":
        raise ProviderError(f"the response is {status}", transient=True, cost=usage.cost)
    return Reply(text, Stop.COMPLETE, usage)


@contextlib.contextmanager
def _errors() -> Generator[None]:
    """The SDK's failures as provider errors, for the gateway's retries and the batcher."""
    try:
        yield
    except openai.APITimeoutError as exc:
        if isinstance(exc.__cause__, httpx2.ConnectTimeout):
            # The connection never opened, so nothing was sent: worth another attempt.
            raise ProviderError("APITimeoutError while connecting", transient=True) from exc
        raise ProviderError(type(exc).__name__, timeout=True) from exc
    except openai.APIConnectionError as exc:  # it may have been sent: billed or not
        raise ProviderError(type(exc).__name__, transient=True, unanswered=True) from exc
    except openai.RateLimitError as exc:
        if _billing(exc.body):
            raise ProviderError(f"{type(exc).__name__}: {exc}", account=True) from exc
        raise ProviderError(type(exc).__name__, transient=True) from exc
    except openai.InternalServerError as exc:
        raise ProviderError(type(exc).__name__, transient=True) from exc
    except openai.APIError as exc:
        account = getattr(exc, "status_code", None) in ACCOUNT_STATUSES
        raise ProviderError(f"{type(exc).__name__}: {exc}", account=account) from exc


def _tag(keys: Iterable[str]) -> str:
    """What identifies a submission: its request keys, in any order."""
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()


def _answer(line: dict[str, Any], batch_id: str) -> Answered:
    """One line of a batch's output or error file: the reply; an account error, never asked
    again; None for one the batch did not answer otherwise (failed, expired, cancelled, declined
    for its content), which is asked again as a normal call."""
    response = line.get("response") or {}
    code = response.get("status_code")
    body = response.get("body") or {}
    if code == 200:
        try:
            return reply_of(body, None, batch=True)
        except ProviderError:
            return None
    error = body.get("error") or line.get("error") or {}
    if code in ACCOUNT_STATUSES or (code == 429 and _billing(error)):
        return ProviderError(f"{error.get('code') or code} in batch {batch_id}", account=True)
    return None


class OpenAIProvider:
    name = "openai"
    one_model = True  # a batch's input file may name one model only (OpenAI's Batch API guide)

    def __init__(self, api_key: str | None = None, client: Any = None) -> None:
        # The gateway owns retries; the SDK's own would multiply them.
        self.client = client or AsyncOpenAI(base_url=BASE_URL, api_key=api_key, max_retries=0)

    def routing(self, role: RoleConfig) -> tuple[str, ...]:
        return ()

    async def complete(self, role: RoleConfig, request: Request) -> Reply:
        with _errors():
            try:
                response = await self.client.responses.create(**responses_request(role, request))
            except openai.BadRequestError as exc:
                if (category := _declined(exc.body)) is None:
                    raise
                # An answered error, not billed.
                usage = Usage(model=role.model, via="openai", effort=role.effort, cost=0.0)
                said = json.dumps(exc.body, default=str)
                return Reply("", Stop.REFUSED, usage, refusal=category, raw=said)
        return reply_of(response.model_dump(), role)

    # The Batch API (models.md, batches): `custom_id` is the gateway's request key.

    async def submit(self, requests: list[tuple[str, RoleConfig, Request]]) -> str:
        """The batch's id. A submission whose reply was lost is found again by its tag rather than
        sent twice (P24): before it is sent, and once more when the batch's creation got no
        reply."""
        tag = _tag(key for key, _, _ in requests)
        with _errors():
            if (found := await self._sent(tag)) is not None:
                return found
            lines = [
                json.dumps(
                    {
                        "custom_id": key,
                        "method": "POST",
                        "url": ENDPOINT,
                        "body": responses_request(role, request),
                    },
                    ensure_ascii=False,
                )
                for key, role, request in requests
            ]
            upload = ("requests.jsonl", "\n".join(lines).encode(), "application/jsonl")
            file = await self.client.files.create(file=upload, purpose="batch")
            try:
                batch = await self.client.batches.create(
                    input_file_id=file.id,
                    endpoint=ENDPOINT,
                    completion_window="24h",
                    metadata={TAG: tag},
                )
            except (openai.APITimeoutError, openai.APIConnectionError):
                if (found := await self._sent(tag)) is not None:
                    return found
                raise
        return batch.id

    async def _sent(self, tag: str) -> str | None:
        """A batch of these requests already sent and still running, among the newest."""
        page = await self.client.batches.list(limit=LISTED)
        for batch in page.data:
            if (batch.metadata or {}).get(TAG) == tag and _status(batch.status) == "in_progress":
                return batch.id
        return None

    async def status(self, batch_id: str) -> str:
        """`in_progress`, `canceling` or `ended`; `gone` when the API no longer has it."""
        with _errors():
            try:
                batch = await self.client.batches.retrieve(batch_id)
            except openai.NotFoundError:
                return "gone"
        return _status(batch.status)

    async def cancel(self, batch_id: str) -> None:
        with _errors():
            await self.client.batches.cancel(batch_id)

    async def results(self, batch_id: str) -> dict[str, Answered]:
        """Each request's answer, by its key, from the batch's output and error files
        (`_answer`). A request neither file names was not answered."""
        found: dict[str, Answered] = {}
        with _errors():
            batch = await self.client.batches.retrieve(batch_id)
            if batch.status == "failed":
                log.warning("batch %s failed: %s", batch_id, batch.errors)
            for file_id in (batch.output_file_id, batch.error_file_id):
                if file_id is None:
                    continue
                content = await self.client.files.content(file_id)
                for text in content.text.splitlines():
                    if text.strip():
                        line = json.loads(text)
                        found[line["custom_id"]] = _answer(line, batch_id)
        return found
