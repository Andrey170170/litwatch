"""The model call journal: durable accounting and exact-request reuse."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.llm.types import Usage

RESULTS_KEPT = timedelta(days=28)  # how long a batch's calls are waited on and accounted for
RETALLY_LOCK = 7213912  # advisory lock namespace, beside jobs.lock.RUN_LOCK


class CallJournal:
    """One run's calls. Give it a connection of its own: concurrent calls write here while
    the job's connection is inside a transaction."""

    def __init__(self, conn: psycopg.AsyncConnection, run_id: int) -> None:
        self.conn, self.run_id = conn, run_id
        self._retallying = asyncio.Lock()  # the connection holds one retally's lock at a time

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

    async def batched(self, call_ids: list[int], batch_id: str, provider: str) -> None:
        """The calls went out in `provider`'s batch `batch_id`."""
        await self.conn.execute(
            "UPDATE inference_calls SET batch_id = %s, "
            "usage = usage || jsonb_build_object('via', %s::text) WHERE id = ANY(%s)",
            (batch_id, provider, call_ids),
        )

    async def in_batch(self, key: str) -> dict[str, Any] | None:
        """The newest call sent in a batch with this request and not yet settled (no output, no
        error): a run started again waits on its batch instead of paying again. Anthropic keeps
        a batch's results for 29 days; `RESULTS_KEPT` stays inside that."""
        return await (
            await self.conn.execute(
                "SELECT id, batch_id FROM inference_calls WHERE request_key = %s "
                "AND batch_id IS NOT NULL AND output IS NULL AND error IS NULL "
                "AND started_at > now() - %s ORDER BY id DESC LIMIT 1",
                (key, RESULTS_KEPT),
            )
        ).fetchone()

    async def open_batches(self) -> list[dict[str, Any]]:
        """The batches with calls not yet answered or charged, with the provider each went to:
        a dead run's, or one given up on after a deadline, whose charges are read when they
        end."""
        return await (
            await self.conn.execute(
                "SELECT batch_id, min(usage->>'via') AS provider FROM inference_calls "
                "WHERE batch_id IS NOT NULL AND output IS NULL AND error IS NULL "
                "AND usage->>'cost' IS NULL AND started_at > now() - %s "
                "GROUP BY batch_id ORDER BY batch_id",
                (RESULTS_KEPT,),
            )
        ).fetchall()

    async def gone(self, batch_id: str) -> None:
        """Batch `batch_id` is gone from its provider (deleted, or the key is another
        workspace's): it never answers, so its calls not yet settled are settled with that."""
        await self.conn.execute(
            "UPDATE inference_calls SET error = 'provider: the batch is gone' "
            "WHERE batch_id = %s AND output IS NULL AND error IS NULL "
            "AND usage->>'cost' IS NULL",
            (batch_id,),
        )

    async def answered(self, batch_id: str, key: str, usage: Usage | None) -> None:
        """An answer of batch `batch_id` that no call awaited: its charge, on the calls that
        asked for it and have none yet, so the batch is accounted for in full. None: the batch
        did not answer it, and its call is settled with that."""
        if usage is None:
            await self.conn.execute(
                "UPDATE inference_calls SET error = 'provider: the batch did not answer it' "
                "WHERE batch_id = %s AND request_key = %s AND output IS NULL AND error IS NULL "
                "AND usage->>'cost' IS NULL",
                (batch_id, key),
            )
            return
        cur = await self.conn.execute(
            "UPDATE inference_calls SET usage = %s, responded_at = clock_timestamp() "
            "WHERE id = (SELECT min(id) FROM inference_calls WHERE batch_id = %s "
            "AND request_key = %s AND usage->>'cost' IS NULL) RETURNING run_id",
            (Jsonb(usage.model_dump(mode="json")), batch_id, key),
        )
        await self._retally(await cur.fetchone())

    async def record(self, call_id: int, usage: Usage) -> None:
        cur = await self.conn.execute(
            "UPDATE inference_calls SET usage = %s, responded_at = clock_timestamp() "
            "WHERE id = %s RETURNING run_id",
            (Jsonb(usage.model_dump(mode="json")), call_id),
        )
        await self._retally(await cur.fetchone())

    async def _retally(self, row: dict[str, Any] | None) -> None:
        """A charge on another run's call (a dead run's batch, read later) is in that run's cost
        and calls by role, as its own run's close would have put it."""
        if row is None or row["run_id"] == self.run_id:
            return
        # One retally of a run at a time, each reading the calls once it holds the lock, so a
        # concurrent one cannot write a total older than its own. The calls on this connection
        # retally in turn: one waiting for a lock would hold up another's release.
        lock = (RETALLY_LOCK, row["run_id"])
        async with self._retallying:
            await self.conn.execute("SELECT pg_advisory_lock(%s, %s)", lock)
            try:
                await self._retally_now(row["run_id"])
            finally:
                await self.conn.execute("SELECT pg_advisory_unlock(%s, %s)", lock)

    async def _retally_now(self, run_id: int) -> None:
        await self.conn.execute(
            "UPDATE runs SET cost = t.cost, counts = jsonb_set(counts, '{calls}', t.calls) "
            "FROM (SELECT round(coalesce(sum(cost), 0)::numeric, 4) AS cost, "
            "coalesce(jsonb_object_agg(operation, jsonb_build_object('calls', calls, "
            "'cost', cost)), '{}') AS calls FROM (SELECT operation, count(*) AS calls, "
            "coalesce(sum((usage->>'cost')::numeric), 0)::float AS cost FROM inference_calls "
            "WHERE run_id = %(run)s GROUP BY operation) roles) t WHERE runs.id = %(run)s",
            {"run": run_id},
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
    estimate goes by. Calls under another provider or effort, or answered in a batch, are left
    out, as they cost differently (an estimate is of normal calls)."""
    row = await (
        await conn.execute(
            "SELECT avg(cost) AS cost FROM (SELECT (usage->>'cost')::float AS cost "
            "FROM inference_calls WHERE operation = %s AND usage->>'model' = %s "
            "AND usage->>'via' = %s AND usage->>'effort' IS NOT DISTINCT FROM %s "
            "AND usage->>'cost' IS NOT NULL AND NOT coalesce((usage->>'reused')::boolean, false) "
            "AND usage->>'service_tier' IS DISTINCT FROM 'batch' ORDER BY id DESC LIMIT %s) recent",
            (role, model, provider, effort, last),
        )
    ).fetchone()
    return row["cost"]
