"""OpenAI's models on OpenAI's own API: the Responses API with structured output for normal
calls, and the Batch API for batched ones (`llm.batches`), each priced from `llm.prices`.

Its explicit cache controls need GPT-5.6 or later; the models priced (`prices.OPENAI`) are the
GPT-6 and GPT-6.x families litwatch may route. A model without a listed price is answered with
its charge unknown."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Generator
from typing import Any

import httpx2  # the HTTP client this SDK is built on
import openai
from openai import AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema

from litwatch.config import RoleConfig
from litwatch.llm.batches import Answered, Staged
from litwatch.llm.prices import openai_cost, openai_model
from litwatch.llm.types import ACCOUNT_STATUSES, ProviderError, Reply, Request, Stop, Usage

log = logging.getLogger(__name__)

# Explicit, so an `OPENAI_BASE_URL` in the environment never sends the calls elsewhere.
BASE_URL = "https://api.openai.com/v1"
ENDPOINT = "/v1/responses"
# Normal calls ask for it, so a reply is priced only at the rate it was served at.
TIER = "default"
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
    "failed": "ended",  # its input was refused: nothing is answered
}
# A batch before its id is known: its input file's, which the batch names (`_find`).
FILE_REF = "openai-file:"
LISTED = 100  # batches per page, looking for the one made from a file


def _status(status: str) -> str:
    """A status this module does not know is read as still running: polled until it ends."""
    return STATUSES.get(status, "in_progress")


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
    """The reply of a response, as the API sends it. It is priced at the rate it was served at:
    the batch rate for an answer from a batch, the standard one for a normal call served at
    `TIER`; at any other tier its charge is unknown. Without the role (an answer of a batch no
    call awaits) the model is the response's and the effort unknown. A response that failed is
    a provider error that carries its usage, but for one declined for its content, which is a
    refusal."""
    model = role.model if role else openai_model(body.get("model") or "")
    tier = "batch" if batch else body.get("service_tier")
    u = body.get("usage")
    cost = None
    cache_read = cache_write = reasoning = None
    if u:
        details = u.get("input_tokens_details") or {}
        cache_read = details.get("cached_tokens", 0)
        cache_write = details.get("cache_write_tokens", 0)
        reasoning = (u.get("output_tokens_details") or {}).get("reasoning_tokens")
        if batch or tier == TIER:
            cost = openai_cost(
                model, u["input_tokens"], cache_read, cache_write, u["output_tokens"], batch=batch
            )
        else:
            log.warning("%s served at the %s tier, its cost is not recorded", model, tier)
    usage = Usage(
        model=model,
        via="openai",
        effort=role.effort if role else None,
        prompt_tokens=u["input_tokens"] if u else 0,
        completion_tokens=u["output_tokens"] if u else 0,
        cost=cost,
        generation_id=body.get("id"),
        provider="openai",
        service_tier=tier,
        cached_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning,
    )
    status = body.get("status")
    if status == "failed":
        error = body.get("error") or {}
        if refused := _refusal(error, usage):
            return refused
        transient = error.get("code") in ("server_error", "rate_limit_exceeded")
        raise ProviderError(f"response failed: {error}", transient=transient, usage=usage)
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
        raise ProviderError(f"the response is {status}", transient=True, usage=usage)
    return Reply(text, Stop.COMPLETE, usage)


def _refusal(error: Any, usage: Usage) -> Reply | None:
    """An error about the request's content as the refusal it is, with what it was billed: an
    HTTP 400's, a failed response's, a batch's error line's alike."""
    if (category := _declined(error)) is None:
        return None
    return Reply("", Stop.REFUSED, usage, refusal=category, raw=json.dumps(error, default=str))


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


def _answer(line: dict[str, Any], batch_id: str, model: str) -> Answered:
    """One line of a batch's output or error file, of `model`'s batch: the reply, or a refusal
    for a request declined for its content; a failure it billed (`ProviderError.usage`); an
    account error, never asked again; None for one the batch did not answer otherwise (failed,
    expired, cancelled), which is asked again as a normal call."""
    response = line.get("response") or {}
    code = response.get("status_code")
    body = response.get("body") or {}
    if code == 200:
        try:
            return reply_of(body, None, batch=True)
        except ProviderError as exc:
            return exc
    error = body.get("error") or line.get("error") or {}
    # An answered error, not billed.
    if refused := _refusal(error, Usage(model=model, via="openai", cost=0.0)):
        return refused
    if code in ACCOUNT_STATUSES or (code == 429 and _billing(error)):
        return ProviderError(f"{error.get('code') or code} in batch {batch_id}", account=True)
    return None


class OpenAIProvider:
    name = "openai"
    one_model = True  # a batch's input file may name one model only (OpenAI's Batch API guide)

    def __init__(self, api_key: str | None = None, client: Any = None) -> None:
        # The gateway owns retries; the SDK's own would multiply them.
        self.client = client or AsyncOpenAI(base_url=BASE_URL, api_key=api_key, max_retries=0)
        self._made: dict[str, str] = {}  # a file reference's batch, once found (`_find`)

    def routing(self, role: RoleConfig) -> tuple[str, ...]:
        return ()

    async def complete(self, role: RoleConfig, request: Request) -> Reply:
        body: dict[str, Any] = {**responses_request(role, request), "service_tier": TIER}
        with _errors():
            try:
                response = await self.client.responses.create(**body)
            except openai.BadRequestError as exc:
                usage = Usage(model=role.model, via="openai", effort=role.effort, cost=0.0)
                if (refused := _refusal(exc.body, usage)) is None:  # an answered error, not billed
                    raise
                return refused
        return reply_of(response.model_dump(), role)

    # The Batch API (models.md, batches): `custom_id` is the gateway's request key.

    async def submit(self, requests: list[tuple[str, RoleConfig, Request]], staged: Staged) -> str:
        """The batch's id. Its input file is uploaded first and its reference `staged`, so a
        batch whose creation lost its reply is found again by its file rather than sent twice
        (P24). A lookup that cannot tell whether it was created raises `unanswered`, leaving the
        reference for recovery to resolve."""
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
        try:
            with _errors():
                file = await self.client.files.create(file=upload, purpose="batch")
        except ProviderError as exc:
            if exc.account:
                raise
            # No batch can come of a file not known to be uploaded: the calls go normally.
            raise ProviderError(f"the batch's file was not uploaded: {exc}") from exc
        await staged(FILE_REF + file.id)
        with _errors():
            try:
                batch = await self.client.batches.create(
                    input_file_id=file.id, endpoint=ENDPOINT, completion_window="24h"
                )
            except (openai.APITimeoutError, openai.APIConnectionError) as lost:
                try:
                    made = await self._find(file.id)
                except Exception as exc:
                    raise ProviderError(
                        f"batch of {file.id} may have been made: {exc}", unanswered=True
                    ) from lost
                if made is None:
                    raise ProviderError(f"no batch was made of {file.id}") from lost
                return made
        return batch.id

    async def _find(self, file_id: str) -> str | None:
        """The batch made of the file, in any state, looking through every page; None when no
        batch was."""
        after = ""
        while True:
            page = await self.client.batches.list(limit=LISTED, after=after or openai.omit)
            for batch in page.data:
                if batch.input_file_id == file_id:
                    return batch.id
            if not page.data or not page.has_more:
                return None
            after = page.data[-1].id

    async def _batch(self, handle: str) -> Any:
        """The batch by its id or its file reference (`FILE_REF`); None when there is none."""
        batch_id: str | None = self._made.get(handle, handle)
        if batch_id is not None and batch_id.startswith(FILE_REF):
            batch_id = await self._find(batch_id.removeprefix(FILE_REF))
            if batch_id is None:
                return None
            self._made[handle] = batch_id
        try:
            return await self.client.batches.retrieve(batch_id)
        except openai.NotFoundError:
            return None

    async def status(self, batch_id: str) -> str:
        """`in_progress`, `canceling` or `ended`; `gone` when the API has no such batch, by its
        id or its file (never made, deleted, or the key is another project's)."""
        with _errors():
            batch = await self._batch(batch_id)
        return "gone" if batch is None else _status(batch.status)

    async def cancel(self, batch_id: str) -> None:
        with _errors():
            if (batch := await self._batch(batch_id)) is not None:
                await self.client.batches.cancel(batch.id)

    async def results(self, batch_id: str) -> dict[str, Answered]:
        """Each request's answer, by its key, from the batch's output and error files
        (`_answer`). A request neither file names was not answered."""
        found: dict[str, Answered] = {}
        with _errors():
            batch = await self._batch(batch_id)
            if batch is None:
                raise ProviderError(f"batch {batch_id} is gone")
            if batch.status == "failed":
                log.warning("batch %s failed: %s", batch_id, batch.errors)
            model = openai_model(batch.model or "")
            for file_id in (batch.output_file_id, batch.error_file_id):
                if file_id is None:
                    continue
                content = await self.client.files.content(file_id)
                # JSON Lines end with "\n" alone: `splitlines` would also break a line at a
                # U+2028 inside one of its strings.
                for text in content.text.split("\n"):
                    if text.strip():
                        line = json.loads(text)
                        found[line["custom_id"]] = _answer(line, batch_id, model)
        return found
