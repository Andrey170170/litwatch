"""The model call journal: durable accounting and exact-request reuse."""

from __future__ import annotations

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

    async def record(self, call_id: int, usage: Usage) -> None:
        await self.conn.execute(
            "UPDATE inference_calls SET usage = %s, responded_at = clock_timestamp() WHERE id = %s",
            (Jsonb(usage.model_dump(mode="json")), call_id),
        )

    async def accept(self, call_id: int, output: dict[str, Any]) -> None:
        await self.conn.execute(
            "UPDATE inference_calls SET output = %s WHERE id = %s", (Jsonb(output), call_id)
        )

    async def reject(self, call_id: int, error: str) -> None:
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
