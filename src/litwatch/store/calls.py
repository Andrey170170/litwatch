"""The model call journal: durable accounting and exact-request reuse; and what each provider
last said, its latest account error and reply (`provider_observations`)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.llm.types import Usage

RESULTS_KEPT = timedelta(days=28)  # how long a batch's calls are waited on and accounted for
RETALLY_LOCK = 7213912  # advisory lock namespace, beside jobs.lock.RUN_LOCK
EVAL = "eval"  # an evaluation run's kind: its calls say nothing to the status document

# What a provider said, kept in `provider_observations` by the same statement as the call row it
# came with (`c`, returning `responded_at`), so the two never part and the reply's time is its
# row's own. Observations from other processes can commit out of order: each keeps the later.
_REPLIED = (
    "o AS (INSERT INTO provider_observations AS o (provider, replied_at) "
    "SELECT %(via)s::text, responded_at FROM c WHERE %(own)s "
    "ON CONFLICT (provider) DO UPDATE SET "
    "replied_at = greatest(o.replied_at, EXCLUDED.replied_at))"
)
_ACCOUNT_ERROR = (
    "INSERT INTO provider_observations AS o (provider, account_error, account_error_at) "
    "SELECT %(provider)s::text, %(error)s, clock_timestamp() {source} "
    "ON CONFLICT (provider) DO UPDATE SET "
    "account_error = CASE WHEN o.account_error_at > EXCLUDED.account_error_at "
    "THEN o.account_error ELSE EXCLUDED.account_error END, "
    "account_error_at = greatest(o.account_error_at, EXCLUDED.account_error_at)"
)


class CallJournal:
    """One run's calls. Give it a connection of its own: concurrent calls write here while
    the job's connection is inside a transaction. Without `observes` (an evaluation run, which
    writes its own tables only) it keeps no provider observations."""

    def __init__(
        self, conn: psycopg.AsyncConnection, run_id: int, *, observes: bool = True
    ) -> None:
        self.conn, self.run_id, self.observes = conn, run_id, observes
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

    async def start(
        self, role: str, model: str, key: str, paper_id: str | None, via: str | None = None
    ) -> int:
        """A call about to be sent, to `model` by provider `via`: its row, before any reply."""
        row = await (
            await self.conn.execute(
                "INSERT INTO inference_calls (run_id, operation, request_key, usage, paper_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    self.run_id,
                    role,
                    key,
                    Jsonb(Usage(model=model, via=via).model_dump(mode="json")),
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

    async def answered(
        self, batch_id: str, key: str, usage: Usage | None, error: str | None = None
    ) -> None:
        """An answer of batch `batch_id` that no call awaited: its charge, on the calls that
        asked for it and have none yet, so the batch is accounted for in full. None: the batch
        did not answer it, and its call is settled with that, or with `error` (an account
        error)."""
        if usage is None:
            await self.conn.execute(
                "UPDATE inference_calls SET error = %s "
                "WHERE batch_id = %s AND request_key = %s AND output IS NULL AND error IS NULL "
                "AND usage->>'cost' IS NULL",
                (error or "provider: the batch did not answer it", batch_id, key),
            )
            return
        cur = await self.conn.execute(
            "WITH c AS (UPDATE inference_calls SET usage = %(usage)s, "
            "responded_at = clock_timestamp() WHERE id = (SELECT min(id) FROM inference_calls "
            "WHERE batch_id = %(batch)s AND request_key = %(key)s AND usage->>'cost' IS NULL) "
            f"RETURNING run_id, responded_at), {_REPLIED} SELECT run_id FROM c",
            {"usage": Jsonb(usage.model_dump(mode="json")), "batch": batch_id, "key": key}
            | self._reply(usage),
        )
        await self._retally(await cur.fetchone())

    async def record(self, call_id: int, usage: Usage) -> None:
        """A reply came for the call: its accounting, whether or not the answer is usable, and
        that the provider replied."""
        cur = await self.conn.execute(
            "WITH c AS (UPDATE inference_calls SET usage = %(usage)s, "
            "responded_at = clock_timestamp() WHERE id = %(id)s RETURNING run_id, responded_at), "
            f"{_REPLIED} SELECT run_id FROM c",
            {"usage": Jsonb(usage.model_dump(mode="json")), "id": call_id} | self._reply(usage),
        )
        await self._retally(await cur.fetchone())

    def _reply(self, usage: Usage) -> dict[str, Any]:
        """`_REPLIED`'s parameters: a reply shared from another call is not the provider's."""
        own = self.observes and usage.via is not None and not usage.reused
        return {"via": usage.via, "own": own}

    async def account_error(self, provider: str, error: str) -> None:
        """The provider refused a batch, or a request in one, for the account (models.md, the
        gateway), seen by its batcher whether or not a call waits on it. A normal call's is its
        `reject`."""
        if not self.observes:
            return
        await self.conn.execute(
            _ACCOUNT_ERROR.format(source=""), {"provider": provider, "error": error[:2000]}
        )

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

    async def reject(
        self, call_id: int, error: str, reply: str | None = None, *, account: str | None = None
    ) -> None:
        """The call got no usable answer; with `account`, that provider refused it for the
        account, which is observed with it (`observes`)."""
        account = account if self.observes else None
        update = "UPDATE inference_calls SET error = %(error)s, reply = %(reply)s WHERE id = %(id)s"
        observed = _ACCOUNT_ERROR.format(source="FROM c")
        await self.conn.execute(
            f"WITH c AS ({update} RETURNING id), o AS ({observed}) SELECT 1" if account else update,
            {"error": error, "reply": reply, "id": call_id, "provider": account},
        )

    async def refused(self, key: str, paper_id: str | None) -> bool:
        """Whether a call of this identical request about the paper was refused, in any run."""
        row = await (
            await self.conn.execute(
                "SELECT 1 FROM inference_calls WHERE paper_id IS NOT DISTINCT FROM %s "
                "AND request_key = %s AND error LIKE 'refused%%' LIMIT 1",
                (paper_id, key),
            )
        ).fetchone()
        return row is not None

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


async def charged_prices(
    conn: psycopg.AsyncConnection, provider: str, model: str, last: int = 200
) -> tuple[float | None, float | None]:
    """The highest price per input token and per output token the model's latest `last` billed
    calls on `provider` were charged, in USD, or None where none tells. A call's charge bounds
    each price it paid (charge / input tokens bounds the input price, charge / output tokens
    the output price), the tightest such bound per serving provider holds for it, and the
    highest across the serving providers holds for the model. A call with cached input left out
    of the input price, as caching discounts it."""
    row = await (
        await conn.execute(
            """
            WITH recent AS (
                SELECT (usage->>'cost')::float AS cost,
                       (usage->>'prompt_tokens')::float AS tokens_in,
                       (usage->>'completion_tokens')::float AS tokens_out,
                       coalesce((usage->>'cached_tokens')::int, 0) AS cached,
                       coalesce(usage->>'provider', '') AS served_by
                FROM inference_calls
                WHERE usage->>'model' = %s AND usage->>'via' = %s
                  AND usage->>'cost' IS NOT NULL
                  AND NOT coalesce((usage->>'reused')::boolean, false)
                  AND usage->>'service_tier' IS DISTINCT FROM 'batch'
                ORDER BY id DESC LIMIT %s
            ), per AS (
                SELECT min(cost / tokens_in) FILTER (WHERE tokens_in > 0 AND cached = 0) AS p_in,
                       min(cost / tokens_out) FILTER (WHERE tokens_out > 0) AS p_out
                FROM recent GROUP BY served_by
            )
            SELECT max(p_in) AS p_in, max(p_out) AS p_out FROM per
            """,
            (model, provider, last),
        )
    ).fetchone()
    return row["p_in"], row["p_out"]


async def observations(conn: psycopg.AsyncConnection) -> dict[str, dict[str, Any]]:
    """Each provider's latest account error and latest reply, by provider."""
    rows = await (await conn.execute("SELECT * FROM provider_observations")).fetchall()
    return {r["provider"]: r for r in rows}


async def outcomes(conn: psycopg.AsyncConnection, run: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per provider, the run's calls that failed without an answer (provider errors and
    timeouts), and the replies it sent while the run went on, whichever run's calls they
    settled (a dead run's batch, read in this one; reused replies and evaluations' calls, a
    replay job's included, left out)."""
    rows = await (
        await conn.execute(
            "SELECT provider, sum(failed)::int AS failed, sum(replied)::int AS replied FROM ("
            "SELECT usage->>'via' AS provider, 1 AS failed, 0 AS replied FROM inference_calls "
            "WHERE run_id = %(run)s AND (error LIKE 'provider%%' OR error LIKE 'timeout%%') "
            "UNION ALL SELECT usage->>'via', 0, 1 FROM inference_calls "
            "WHERE responded_at BETWEEN %(since)s AND %(until)s "
            "AND NOT coalesce((usage->>'reused')::boolean, false) "
            "AND run_id NOT IN (SELECT r.id FROM runs r LEFT JOIN jobs j ON j.id = r.job_id "
            "WHERE r.kind = %(eval)s OR j.kind = 'replay')) seen "
            "WHERE provider IS NOT NULL GROUP BY provider",
            {
                "run": run["id"],
                "since": run["started_at"],
                "until": run["finished_at"],
                "eval": EVAL,
            },
        )
    ).fetchall()
    return {r["provider"]: {"failed": r["failed"], "replied": r["replied"]} for r in rows}
