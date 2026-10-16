"""The model call journal: durable accounting and exact-request reuse."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.llm.types import Usage


class CallJournal:
    """One run's calls. Give it a connection of its own: concurrent calls write here while
    the job's connection is inside a transaction."""

    def __init__(self, conn: psycopg.AsyncConnection, run_id: int) -> None:
        self.conn, self.run_id = conn, run_id

    async def cached(self, key: str) -> dict[str, Any] | None:
        return await (
            await self.conn.execute(
                "SELECT id, output, usage FROM inference_calls "
                "WHERE request_key = %s AND output IS NOT NULL AND invalidated_at IS NULL "
                "ORDER BY id LIMIT 1",
                (key,),
            )
        ).fetchone()

    async def start(self, role: str, model: str, key: str, paper_id: str | None) -> int:
        row = await (
            await self.conn.execute(
                "INSERT INTO inference_calls (run_id, operation, request_key, usage, paper_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    self.run_id,
                    role,
                    key,
                    Jsonb(Usage(model=model).model_dump(mode="json")),
                    paper_id,
                ),
            )
        ).fetchone()
        return row["id"]

    async def batched(self, call_ids: list[int], batch_id: str) -> None:
        """The calls went out in the provider's batch `batch_id`."""
        await self.conn.execute(
            "UPDATE inference_calls SET batch_id = %s WHERE id = ANY(%s)", (batch_id, call_ids)
        )

    async def in_batch(self, key: str) -> dict[str, Any] | None:
        """The newest call of the last day sent in a batch with this request and not yet settled
        (no output, no error): a run started again waits on its batch instead of paying again.
        Older batches' answers are gone or soon will be."""
        return await (
            await self.conn.execute(
                "SELECT id, batch_id FROM inference_calls WHERE request_key = %s "
                "AND batch_id IS NOT NULL AND output IS NULL AND error IS NULL "
                "AND started_at > now() - interval '1 day' ORDER BY id DESC LIMIT 1",
                (key,),
            )
        ).fetchone()

    async def open_batches(self) -> list[dict[str, Any]]:
        """The batches of the last day with calls not yet answered or charged, with a role
        that asked in each: a dead run's, whose charges are read when they end."""
        return await (
            await self.conn.execute(
                "SELECT batch_id, min(operation) AS role FROM inference_calls "
                "WHERE batch_id IS NOT NULL AND output IS NULL AND error IS NULL "
                "AND usage->>'cost' IS NULL AND started_at > now() - interval '1 day' "
                "GROUP BY batch_id ORDER BY batch_id"
            )
        ).fetchall()

    async def answered(self, batch_id: str, key: str, usage: Usage) -> None:
        """An answer of batch `batch_id` that no call awaited: its charge, on the calls that
        asked for it and have none yet, so the batch is accounted for in full."""
        await self.conn.execute(
            "UPDATE inference_calls SET usage = %s, responded_at = clock_timestamp() "
            "WHERE id = (SELECT min(id) FROM inference_calls WHERE batch_id = %s "
            "AND request_key = %s AND usage->>'cost' IS NULL)",
            (Jsonb(usage.model_dump(mode="json")), batch_id, key),
        )

    async def record(self, call_id: int, usage: Usage) -> None:
        await self.conn.execute(
            "UPDATE inference_calls SET usage = %s, responded_at = clock_timestamp() WHERE id = %s",
            (Jsonb(usage.model_dump(mode="json")), call_id),
        )

    async def accept(self, call_id: int, output: dict[str, Any]) -> None:
        await self.conn.execute(
            "UPDATE inference_calls SET output = %s WHERE id = %s", (Jsonb(output), call_id)
        )

    async def reject(self, call_id: int, error: str, reply: str | None = None) -> None:
        await self.conn.execute(
            "UPDATE inference_calls SET error = %s, reply = %s WHERE id = %s",
            (error, reply, call_id),
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

    async def by_role(self) -> dict[str, dict[str, float]]:
        """Calls and cost per role in this run."""
        rows = await (
            await self.conn.execute(
                "SELECT operation, count(*) AS calls, "
                "coalesce(sum((usage->>'cost')::numeric), 0) AS cost "
                "FROM inference_calls WHERE run_id = %s GROUP BY operation",
                (self.run_id,),
            )
        ).fetchall()
        return {r["operation"]: {"calls": r["calls"], "cost": float(r["cost"])} for r in rows}


async def spent_since(conn: psycopg.AsyncConnection, since: datetime) -> dict[str, float]:
    """What every run's calls started since `since` cost, per provider account (`usage.via`).
    A reused reply costs nothing; a call of unknown cost counts nothing."""
    rows = await (
        await conn.execute(
            "SELECT usage->>'via' AS via, sum((usage->>'cost')::numeric) AS cost "
            "FROM inference_calls WHERE started_at >= %s AND usage->>'via' IS NOT NULL "
            "AND usage->>'cost' IS NOT NULL GROUP BY 1",
            (since,),
        )
    ).fetchall()
    return {r["via"]: float(r["cost"]) for r in rows}


async def mean_cost(
    conn: psycopg.AsyncConnection,
    role: str,
    provider: str,
    model: str,
    effort: str | None,
    last: int = 200,
) -> float | None:
    """What a call of `role` on `provider`, `model` and `effort` alike cost on average over
    its latest `last` billed calls (reused replies left out), or None when it has none: what an
    estimate goes by. Calls under another provider or effort are left out, as they cost
    differently."""
    row = await (
        await conn.execute(
            "SELECT avg(cost) AS cost FROM (SELECT (usage->>'cost')::float AS cost "
            "FROM inference_calls WHERE operation = %s AND usage->>'model' = %s "
            "AND usage->>'via' = %s AND usage->>'effort' IS NOT DISTINCT FROM %s "
            "AND usage->>'cost' IS NOT NULL AND NOT coalesce((usage->>'reused')::boolean, false) "
            "ORDER BY id DESC LIMIT %s) recent",
            (role, model, provider, effort, last),
        )
    ).fetchone()
    return row["cost"]
