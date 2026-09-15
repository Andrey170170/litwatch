"""Durable inference accounting and exact-request reuse within database-backed runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextvars import ContextVar
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.models import Usage

log = logging.getLogger(__name__)


def usage_of(completion: Any, model: str) -> Usage:
    """Keep provider-reported billing and cache details, including missing values."""
    raw = completion.model_dump() if hasattr(completion, "model_dump") else vars(completion)
    u = completion.usage
    values = u.model_dump() if hasattr(u, "model_dump") else vars(u) if u else {}
    values = {**(values.get("model_extra") or {}), **values}
    prompt = values.get("prompt_tokens_details") or {}
    output = values.get("completion_tokens_details") or {}
    if not isinstance(prompt, dict):
        prompt = vars(prompt)
    if not isinstance(output, dict):
        output = vars(output)
    return Usage(
        model=model,
        prompt_tokens=values.get("prompt_tokens") or 0,
        completion_tokens=values.get("completion_tokens") or 0,
        cost=values.get("cost"),
        generation_id=raw.get("id"),
        provider=raw.get("provider"),
        service_tier=raw.get("service_tier"),
        cached_tokens=prompt.get("cached_tokens"),
        cache_write_tokens=prompt.get("cache_write_tokens"),
        reasoning_tokens=output.get("reasoning_tokens"),
        cache_discount=values.get("cache_discount", raw.get("cache_discount")),
    )


def request_key(value: dict[str, Any]) -> str:
    # Preserve nested schema/property ordering: it can affect the rendered model input.
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode()).hexdigest()


class CallJournal:
    """One run's calls. Share `lock` with other concurrent writes on this connection."""

    def __init__(self, conn: psycopg.AsyncConnection, run_id: int) -> None:
        self.conn, self.run_id = conn, run_id
        self.lock = asyncio.Lock()

    async def cached(self, key: str) -> dict[str, Any] | None:
        async with self.lock:
            return await (
                await self.conn.execute(
                    "SELECT id, output, usage FROM inference_calls "
                    "WHERE request_key = %s AND output IS NOT NULL AND invalidated_at IS NULL "
                    "ORDER BY id LIMIT 1",
                    (key,),
                )
            ).fetchone()

    async def start(
        self, operation: str, model: str, key: str | None, paper_id: str | None = None
    ) -> int:
        async with self.lock:
            row = await (
                await self.conn.execute(
                    "INSERT INTO inference_calls (run_id, operation, request_key, usage, paper_id) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (
                        self.run_id,
                        operation,
                        key,
                        Jsonb(Usage(model=model).model_dump(mode="json")),
                        paper_id,
                    ),
                )
            ).fetchone()
        return row["id"]

    async def record(self, call_id: int, usage: Usage) -> None:
        async with self.lock:
            await self.conn.execute(
                "UPDATE inference_calls SET usage = %s, responded_at = clock_timestamp() "
                "WHERE id = %s",
                (Jsonb(usage.model_dump(mode="json")), call_id),
            )

    async def accept(self, call_id: int, output: dict[str, Any]) -> None:
        async with self.lock:
            await self.conn.execute(
                "UPDATE inference_calls SET output = %s WHERE id = %s", (Jsonb(output), call_id)
            )

    async def reject(self, call_id: int, error: str) -> None:
        async with self.lock:
            await self.conn.execute(
                "UPDATE inference_calls SET error = %s WHERE id = %s", (error, call_id)
            )

    async def total(self) -> float:
        row = await (
            await self.conn.execute(
                "SELECT coalesce(sum((usage->>'cost')::numeric), 0) AS cost "
                "FROM inference_calls WHERE run_id = %s",
                (self.run_id,),
            )
        ).fetchone()
        return float(row["cost"])


current_journal: ContextVar[CallJournal | None] = ContextVar("call_journal", default=None)


async def recorded_completion(
    client,
    *,
    model: str,
    operation: str,
    request: dict,
    key: str | None = None,
    paper_id: str | None = None,
    timeout_seconds: float | None = None,
):
    """Record the response before parsing, so rejected outputs still have a charge."""
    journal = current_journal.get()
    call_id = await journal.start(operation, model, key, paper_id) if journal else None
    try:
        async with asyncio.timeout(timeout_seconds):
            completion = await client.chat.completions.create(model=model, **request)
        usage = usage_of(completion, model)
        usage.source_call_id = call_id
        if journal:
            await journal.record(call_id, usage)
        log.info("inference operation=%s usage=%s", operation, usage.model_dump_json())
        return completion, usage, call_id
    except Exception as exc:
        # No provider cost is invented for transport failures or timeouts. The pending
        # row also survives process termination, where the outcome may be unknown.
        if journal:
            await journal.reject(call_id, type(exc).__name__)
        raise
