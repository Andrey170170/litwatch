"""The model call journal: durable accounting and exact-request reuse; and what each provider
last said, its latest account error and reply (`provider_observations`).

A call's row opens before it is sent (`start`; a batch's calls all at once, with the batch's
reference, `start_many`) and is settled once. A normal call's by its reply's charge (`record`),
then its output (`accept`) or why it could not be used (`reject`). A batched call's by its
batch's one settlement (`settle_batch`, or `unsent` for a batch not made); the call that waits
on it then saves only its output or why that could not be used, on that row. A row settled is
not settled again, so a batch read twice is accounted for once.

Every unit is the Model gate's bookkeeping (`gates.context.bookkeeping`), not fenced: a call's
charge stands whatever becomes of its item's lease. A run's cost and calls by role are derived
from its rows under its `retally` lock (`tally`), by its close and by each batch settlement that
reaches it alike, so whichever writes last has seen every row. A batch settlement commits its
calls' outcomes, what the provider said with them and the totals of the runs it reached in one
unit, which takes those runs' `retally` locks (in the gate's order) as it opens, before it
touches a row (`_settling`)."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, LiteralString

from psycopg.types.json import Jsonb

from litwatch.core.budget import CallOutcome, CallStart, Usage
from litwatch.gates.context import bookkeeping
from litwatch.gates.database import DatabaseGate, Query, ReadUnit, WriteUnit
from litwatch.store.text import stored

RESULTS_KEPT = timedelta(days=28)  # how long a batch's calls are waited on and accounted for
EVAL = "eval"  # an evaluation run's kind: its calls say nothing to the status document
GATE = "model gate"  # the system attribution the journal's units run under

# The live claims that sent batch `{batch}`'s calls (`w`): each call's run is alive by the claim
# it was opened under (`store.runs.ALIVE`, its `lease_token`), which a take-back of the same item
# replaces, and a P3 row's run too (its calls name no item). A claim of the asking item
# (`%(me)s`) does not count: it is this process's own.
_CLAIMS = (
    "FROM inference_calls l JOIN runs r ON r.id = l.run_id "
    "JOIN work_items w ON w.lease_token = r.lease_token WHERE l.batch_id = {batch} "
    "AND w.state = 'running' AND w.lease_until > clock_timestamp() "
    "AND w.id IS DISTINCT FROM %(me)s"
)
# A batch's identity, from its rows' opening on: its first call's id, whatever it is known by
# (its staged reference, then its id; the same rows move from one to the other together).
_FIRST = "(SELECT min(f.id) FROM inference_calls f WHERE f.batch_id = {batch})"
# A batch's terminal answer to a call, which a call asking again in the same item consumes
# rather than paying for the same answer twice: a refusal, a truncation.
_STOPPED = "(c.error LIKE 'refused%%' OR c.error = 'truncated')"
# The item call `c` served: its own, or, for a P3 row (its calls name no item), its run's.
_ITEM = "coalesce(c.work_id, (SELECT i.work_id FROM runs i WHERE i.id = c.run_id))"
# What a provider said, kept in `provider_observations` by the same statement as the call rows
# it came with (`c`, returning `via`, `replied` and `responded_at`), so the two never part and
# the reply's time is its row's own. Observations from other processes can commit out of
# order: each keeps the later.
_REPLIED = (
    "o AS (INSERT INTO provider_observations AS o (provider, replied_at) "
    "SELECT via, max(responded_at) FROM c WHERE replied AND via IS NOT NULL GROUP BY via "
    "ON CONFLICT (provider) DO UPDATE SET "
    "replied_at = greatest(o.replied_at, EXCLUDED.replied_at))"
)
_ACCOUNT_ERROR = (
    "INSERT INTO provider_observations AS o (provider, account_error, account_error_at) "
    "SELECT %(provider)s::text, %(account)s, clock_timestamp() {source} "
    "ON CONFLICT (provider) DO UPDATE SET "
    "account_error = CASE WHEN o.account_error_at > EXCLUDED.account_error_at "
    "THEN o.account_error ELSE EXCLUDED.account_error END, "
    "account_error_at = greatest(o.account_error_at, EXCLUDED.account_error_at)"
)
# A settlement's usage (`a.usage`, None fields left out) merged into its row's (`c`): the row
# keeps its configured model and effort, as the answer knows its provider's name for the model,
# and no effort.
_MERGED = (
    "c.usage || coalesce(a.usage, '{}') || "
    "jsonb_build_object('model', c.usage->'model', 'effort', c.usage->'effort')"
)
# Settlements as rows (`a`): by request key or by call id.
_SETTLED = (
    "jsonb_to_recordset(%(settled)s) AS a({of}, usage jsonb, error text, reply text, "
    "replied boolean)"
)
# The rows not yet settled (`u`): no output, no error, no reply's time.
_UNSETTLED = "u.output IS NULL AND u.error IS NULL AND u.responded_at IS NULL"


def _settled(outcomes: Iterable[tuple[Any, CallOutcome]], name: str) -> Jsonb:
    """Settlements as `_SETTLED` reads them, each under `name` (its key or its call's id)."""
    return Jsonb(
        [
            {
                name: of,
                "usage": o.usage and o.usage.model_dump(mode="json", exclude_none=True),
                "error": o.error and stored(o.error),
                "reply": o.reply and stored(o.reply, None),
                "replied": o.replied,
            }
            for of, o in outcomes
        ]
    )


class CallJournal:
    """The calls of run `run_id`, or with none the Model gate's own journal, which opens and
    settles the calls of every run's batches. Each read and write is a short unit of its own on
    `db`: calls finish while the job works, and no model call runs inside a unit. Without
    `observes` (an evaluation run, which writes its own tables only) it keeps no provider
    observations, and its calls neither batch nor wait on a batch."""

    def __init__(
        self, db: DatabaseGate, run_id: int | None = None, *, observes: bool = True
    ) -> None:
        self.db, self.run_id, self.observes = db, run_id, observes

    async def _one(self, name: str, query: Query, params: Any = None) -> dict[str, Any] | None:
        """The first row of a read, in a read unit of its own."""
        with bookkeeping(GATE):
            async with self.db.read(f"journal: {name}") as unit:
                return await (await unit.execute(query, params)).fetchone()

    async def _all(self, name: str, query: Query, params: Any = None) -> list[dict[str, Any]]:
        with bookkeeping(GATE):
            async with self.db.read(f"journal: {name}") as unit:
                return await (await unit.execute(query, params)).fetchall()

    @asynccontextmanager
    async def _writing(self, name: str, locks: Iterable[Any] = ()) -> AsyncGenerator[WriteUnit]:
        with bookkeeping(GATE):
            async with self.db.write(f"journal: {name}", locks=locks) as unit:
                yield unit

    async def _write(self, name: str, query: Query, params: Any = None) -> list[dict[str, Any]]:
        """A write, in a write unit of its own; the rows it returns, if any."""
        async with self._writing(name) as unit:
            cur = await unit.execute(query, params)
            return await cur.fetchall() if cur.description else []

    async def cached(self, key: str) -> dict[str, Any] | None:
        return await self._one(
            "cached",
            "SELECT id, output, usage FROM inference_calls "
            "WHERE request_key = %s AND output IS NOT NULL AND invalidated_at IS NULL "
            "ORDER BY id LIMIT 1",
            (key,),
        )

    async def start(self, call: CallStart) -> int:
        """A call about to be sent: its row, before any reply."""
        (call_id,) = await self.start_many([call])
        return call_id

    async def start_many(
        self,
        calls: list[CallStart],
        batch_id: str | None = None,
        opened: Callable[[list[int]], None] | None = None,
    ) -> list[int]:
        """The rows of calls of distinct requests, in their order, opened in one statement, so
        all are or none is; with `batch_id`, naming the batch they go out in (its staged
        reference, before it has an id). `opened` is given their ids before they are
        committed, so whatever it records is there before another unit can read them."""
        async with self._writing("start") as unit:
            cur = await unit.execute(
                "INSERT INTO inference_calls "
                "(run_id, work_id, operation, request_key, usage, paper_id, reserved, batch_id) "
                "SELECT run_id, work_id, operation, key, usage, paper_id, reserved, %(batch)s "
                "FROM jsonb_to_recordset(%(calls)s) AS s(run_id bigint, work_id bigint, "
                "operation text, key text, usage jsonb, paper_id text, reserved double precision) "
                "RETURNING id, request_key",
                {
                    "batch": batch_id,
                    "calls": Jsonb(
                        [
                            c.model_dump(mode="json", exclude={"model", "via", "effort"})
                            | {"usage": _opened(c).model_dump(mode="json")}
                            for c in calls
                        ]
                    ),
                },
            )
            found = {r["request_key"]: r["id"] for r in await cur.fetchall()}
            ids = [found[c.key] for c in calls]
            if opened is not None:
                opened(ids)
        return ids

    async def batched(self, call_ids: list[int], batch_id: str, provider: str) -> None:
        """The calls went out in `provider`'s batch `batch_id`."""
        await self._write(
            "batched",
            "UPDATE inference_calls SET batch_id = %s, "
            "usage = usage || jsonb_build_object('via', %s::text) WHERE id = ANY(%s)",
            (batch_id, provider, call_ids),
        )

    async def adopted(self, reference: str, batch_id: str) -> None:
        """The batch staged as `reference` was found: its calls' rows name its id."""
        await self._write(
            "batch adopted",
            "UPDATE inference_calls SET batch_id = %s WHERE batch_id = %s",
            (batch_id, reference),
        )

    async def in_batch(
        self, key: str, work_id: int | None, owned: list[int] | None = None
    ) -> dict[str, Any] | None:
        """The newest call sent in a batch with this request whose answer is still to come or
        is the asking item's to consume: not yet settled, in a batch the asking gate follows
        (of the identities `owned`) or no live claim but the asking item's (`work_id`) sent, so
        a run started again waits on the batch instead of paying again, and another process's
        live item's batch is left to it; or settled with a refusal or a truncation, sent by the
        asking item (the row's, or a P3 row's run's: its calls name no item; its attempt before
        was killed, or taken back), so it takes that answer's
        path rather than asking the same model again. With its batch's identity (`batch`) and
        the name it goes by now (`batch_id`), its `error` and `usage`. Anthropic keeps a
        batch's results for 29 days; `RESULTS_KEPT` stays inside that."""
        claims = _CLAIMS.format(batch="c.batch_id")
        first = _FIRST.format(batch="c.batch_id")
        return await self._one(
            "in batch",
            f"SELECT c.id, c.batch_id, {first} AS batch, c.error, "
            "c.usage FROM inference_calls c WHERE c.request_key = %(key)s "
            "AND c.batch_id IS NOT NULL AND c.output IS NULL "
            "AND c.started_at > now() - %(kept)s "
            f"AND (c.error IS NULL AND ({first} = ANY(%(owned)s) "
            f"OR NOT EXISTS (SELECT 1 {claims})) "
            f"OR {_ITEM} = %(me)s AND {_STOPPED}) ORDER BY c.id DESC LIMIT 1",
            {"key": key, "kept": RESULTS_KEPT, "me": work_id, "owned": owned or []},
        )

    async def named(self, batch: int) -> str | None:
        """The name the batch of identity `batch` goes by now (its staged reference, or its id):
        its first call's row has it, as all its rows do."""
        row = await self._one(
            "batch named", "SELECT batch_id FROM inference_calls WHERE id = %s", (batch,)
        )
        return None if row is None else row["batch_id"]

    async def open_batches(self, batches: list[int] | None = None) -> list[dict[str, Any]]:
        """The batches with calls not yet settled (of the identities `batches`, else all): each
        one's identity (`batch`), the name it goes by now (`batch_id`), the provider it went to
        and, while a live claim sent it, when the latest such lease lapses (`live_until`; None
        for a dead run's batch, or one given up on): a live claim's batch is left to it, and
        taken over once that claim's lease lapses unrenewed (`ModelGate.recover`)."""
        among = "" if batches is None else "AND b.batch = ANY(%(among)s) "
        claims = _CLAIMS.format(batch="b.batch_id")
        return await self._all(
            "open batches",
            "SELECT b.batch, b.batch_id, b.provider, "
            f"(SELECT max(w.lease_until) {claims}) AS live_until FROM ("
            "SELECT c.batch_id, min(c.id) AS batch, min(c.usage->>'via') AS provider, "
            "bool_or(c.output IS NULL AND c.error IS NULL AND c.responded_at IS NULL) AS open "
            "FROM inference_calls c WHERE c.batch_id IS NOT NULL "
            "AND c.started_at > now() - %(kept)s GROUP BY c.batch_id) b "
            f"WHERE b.open {among}ORDER BY b.batch",
            {"kept": RESULTS_KEPT, "among": batches, "me": None},
        )

    @asynccontextmanager
    async def _settling(
        self, name: str, where: LiteralString, params: Any
    ) -> AsyncGenerator[WriteUnit]:
        """A settlement's unit: holding the `retally` locks of the runs whose rows `where` (on
        `u`) picks and are not yet settled, read just before it opens (a batch's rows are all
        opened before it is made, so none joins them), each of those runs retallied before it
        commits. A row settled meanwhile is left alone, and its run's tally is the same."""
        found = await self._all(
            f"{name}: runs",
            f"SELECT DISTINCT u.run_id FROM inference_calls u WHERE {where} AND {_UNSETTLED}",
            params,
        )
        runs = sorted(r["run_id"] for r in found)
        async with self._writing(name, [("retally", r) for r in runs]) as unit:
            yield unit
            for run_id in runs:
                await tally(unit, run_id)

    async def settle_batch(
        self, batch_id: str, answers: dict[str, CallOutcome], *, rest: str | None = None
    ) -> None:
        """Batch `batch_id`'s outcome, in one unit with its runs' totals (`_settling`): each
        request's answer (`answers`, by request key) on the call that asked for it, if not yet
        settled; with `rest`, every other call of it not yet settled fails with that, its charge
        unknown (the batch ended without naming it, or is gone). An account error it settles
        fails its provider, observed with it: a batch read again settles nothing, and observes
        nothing again. A refusal or truncation on a row whose charge and reply time are
        recorded without it (P3 kept a batch's reply so when no call waited) is put on that row,
        its charge and time kept, so the refusal is known and is not asked for again."""
        replied = "" if self.observes else "AND false"
        async with self._settling("settle batch", "u.batch_id = %s", (batch_id,)) as unit:
            cur = await unit.execute(
                f"WITH a AS (SELECT * FROM {_SETTLED.format(of='key text')}), "
                f"c AS (UPDATE inference_calls c SET usage = {_MERGED}, error = a.error, "
                "reply = coalesce(a.reply, c.reply), responded_at = clock_timestamp() FROM a "
                "WHERE c.batch_id = %(batch)s AND c.request_key = a.key AND c.output IS NULL "
                "AND c.error IS NULL AND c.responded_at IS NULL RETURNING c.usage->>'via' AS via, "
                f"a.replied {replied} AS replied, c.responded_at, c.error), {_REPLIED} "
                "SELECT via, error FROM c WHERE error LIKE 'account:%%' LIMIT 1",
                {"batch": batch_id, "settled": _settled(answers.items(), "key")},
            )
            if (account := await cur.fetchone()) and self.observes:
                await unit.execute(
                    _ACCOUNT_ERROR.format(source=""),
                    {"provider": account["via"], "account": account["error"]},
                )
            if stops := [(k, o) for k, o in answers.items() if _stop(o.error)]:
                await unit.execute(
                    f"WITH a AS (SELECT * FROM {_SETTLED.format(of='key text')}) "
                    "UPDATE inference_calls c SET error = a.error, "
                    "reply = coalesce(a.reply, c.reply) FROM a "
                    "WHERE c.batch_id = %(batch)s AND c.request_key = a.key "
                    "AND c.output IS NULL AND c.error IS NULL AND c.responded_at IS NOT NULL",
                    {"batch": batch_id, "settled": _settled(stops, "key")},
                )
            if rest is not None:
                await unit.execute(
                    "UPDATE inference_calls SET error = %s, responded_at = clock_timestamp() "
                    "WHERE batch_id = %s AND output IS NULL AND error IS NULL "
                    "AND responded_at IS NULL AND request_key <> ALL(%s::text[])",
                    (stored(rest), batch_id, list(answers)),
                )

    async def unsent(
        self, call_ids: list[int], outcome: CallOutcome, *, account: str | None = None
    ) -> None:
        """The calls' batch was not made, or may have been and was not found: their rows settled
        with `outcome` (no charge for a batch not made, an unknown one for a batch that may have
        been), each call asked again on a row of its own, in one unit with their runs' totals;
        with `account`, that provider refused the batch for the account, observed with them."""
        observed = account is not None and self.observes
        async with self._settling("unsent", "u.id = ANY(%s)", (call_ids,)) as unit:
            cur = await unit.execute(
                f"WITH a AS (SELECT * FROM {_SETTLED.format(of='id bigint')}) "
                f"UPDATE inference_calls c SET usage = {_MERGED}, error = a.error, "
                "responded_at = clock_timestamp() FROM a WHERE c.id = a.id "
                "AND c.output IS NULL AND c.error IS NULL RETURNING c.id",
                {"settled": _settled(((i, outcome) for i in call_ids), "id")},
            )
            if await cur.fetchall() and observed and outcome.error:
                await unit.execute(
                    _ACCOUNT_ERROR.format(source=""),
                    {"provider": account, "account": stored(outcome.error)},
                )

    async def record(self, call_id: int, usage: Usage) -> None:
        """A normal call's reply came: its accounting, whether or not the answer is usable, and
        that the provider replied, unless the reply is shared from another call's. A failure's
        charge is its `reject`'s."""
        replied = self.observes and not usage.reused
        await self._write(
            "record",
            "WITH c AS (UPDATE inference_calls SET usage = %(usage)s, "
            "responded_at = clock_timestamp() WHERE id = %(id)s "
            "RETURNING usage->>'via' AS via, %(replied)s AS replied, responded_at), "
            f"{_REPLIED} SELECT 1",
            {"usage": Jsonb(usage.model_dump(mode="json")), "id": call_id, "replied": replied},
        )

    async def account_error(self, provider: str, error: str) -> None:
        """The provider refused a batch's creation, status or results read for the account
        (models.md, the gateway), seen by its batcher whether or not a call waits on it. A
        normal call's is its `reject`; a request's in a batch, its batch's settlement's."""
        if not self.observes:
            return
        await self._write(
            "account error",
            _ACCOUNT_ERROR.format(source=""),
            {"provider": provider, "account": stored(error)},
        )

    async def accept(self, call_id: int, output: dict[str, Any]) -> None:
        """The call's validated output, on its row unless that is settled already: a batched
        call's row is saved by the first of the calls sharing it to validate its reply."""
        await self._write(
            "accept",
            "UPDATE inference_calls SET output = %s "
            "WHERE id = %s AND output IS NULL AND error IS NULL",
            (Jsonb(output), call_id),
        )

    async def reject(
        self,
        call_id: int,
        error: str,
        reply: str | None = None,
        *,
        account: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        """The call got no usable answer, on its row unless that is settled already; with
        `usage`, what the failure was charged, accounted for in the same write and not a reply
        (`record`); with `account`, that provider refused it for the account, which is observed
        with it (`observes`)."""
        account = account if self.observes else None
        charged = ", usage = %(usage)s, responded_at = clock_timestamp()" if usage else ""
        update = (
            f"UPDATE inference_calls SET error = %(error)s, reply = %(reply)s{charged} "
            "WHERE id = %(id)s AND output IS NULL AND error IS NULL"
        )
        observed = _ACCOUNT_ERROR.format(source="FROM c")
        await self._write(
            "reject",
            f"WITH c AS ({update} RETURNING id), o AS ({observed}) SELECT 1" if account else update,
            {
                "error": stored(error),
                "reply": reply and stored(reply, None),
                "id": call_id,
                "provider": account,
                "account": stored(error),
                "usage": Jsonb(usage.model_dump(mode="json")) if usage else None,
            },
        )

    async def refused(self, key: str, paper_id: str | None) -> bool:
        """Whether a call of this identical request about the paper was refused, in any run."""
        row = await self._one(
            "refused",
            "SELECT 1 FROM inference_calls WHERE paper_id IS NOT DISTINCT FROM %s "
            "AND request_key = %s AND error LIKE 'refused%%' LIMIT 1",
            (paper_id, key),
        )
        return row is not None


def _stop(error: str | None) -> bool:
    """Whether a settlement's error is a refusal or a truncation (`_STOPPED`)."""
    return error is not None and (error.startswith("refused") or error == "truncated")


def _opened(call: CallStart) -> Usage:
    """A row's usage before any reply: its configured model, provider and effort."""
    return Usage(model=call.model, via=call.via, effort=call.effort)


async def tally(unit: WriteUnit, run_id: int) -> None:
    """Set the run's cost, and its calls and cost by role, from its calls' rows as they stand:
    under the run's `retally` lock, which the unit holds, so a tally that waited for another
    sees what that one saw and more."""
    await unit.execute(
        "UPDATE runs SET cost = t.cost, counts = jsonb_set(counts, '{calls}', t.calls) "
        "FROM (SELECT round(coalesce(sum(cost), 0)::numeric, 4) AS cost, "
        "coalesce(jsonb_object_agg(operation, jsonb_build_object('calls', calls, "
        "'cost', cost)), '{}') AS calls FROM (SELECT operation, count(*) AS calls, "
        "coalesce(sum((usage->>'cost')::numeric), 0)::float AS cost FROM inference_calls "
        "WHERE run_id = %(run)s GROUP BY operation) roles) t WHERE runs.id = %(run)s",
        {"run": run_id},
    )


async def spent_since(unit: ReadUnit, since: datetime) -> dict[str, float]:
    """What every run's calls started since `since` cost, per provider account (`usage.via`).
    A reused reply costs nothing; a call of unknown cost counts nothing."""
    rows = await (
        await unit.execute(
            "SELECT usage->>'via' AS via, sum((usage->>'cost')::numeric) AS cost "
            "FROM inference_calls WHERE started_at >= %s AND usage->>'via' IS NOT NULL "
            "AND usage->>'cost' IS NOT NULL GROUP BY 1",
            (since,),
        )
    ).fetchall()
    return {r["via"]: float(r["cost"]) for r in rows}


async def mean_cost(
    unit: ReadUnit,
    role: str,
    provider: str,
    model: str,
    effort: str | None,
    last: int = 200,
) -> float | None:
    """What a call of `role` on `provider`, `model` and `effort` alike cost on average over
    its latest `last` billed calls (reused replies and requests rejected unbilled, free and of no
    tokens, left out), or None when it has none: what an estimate goes by. Calls under another
    provider or effort, or answered in a batch, are left out, as they cost differently (an
    estimate is of normal calls)."""
    row = await (
        await unit.execute(
            "SELECT avg(cost) AS cost FROM (SELECT (usage->>'cost')::float AS cost "
            "FROM inference_calls WHERE operation = %s AND usage->>'model' = %s "
            "AND usage->>'via' = %s AND usage->>'effort' IS NOT DISTINCT FROM %s "
            "AND usage->>'cost' IS NOT NULL AND NOT coalesce((usage->>'reused')::boolean, false) "
            "AND usage->>'service_tier' IS DISTINCT FROM 'batch' "
            "AND NOT ((usage->>'cost')::float = 0 "
            "AND coalesce((usage->>'prompt_tokens')::int, 0) = 0) "
            "ORDER BY id DESC LIMIT %s) recent",
            (role, model, provider, effort, last),
        )
    ).fetchone()
    return row["cost"]


async def charged_prices(
    unit: ReadUnit, provider: str, model: str, last: int = 200
) -> tuple[float | None, float | None]:
    """The highest price per input token and per output token the model's latest `last` billed
    calls on `provider` were charged, in USD, or None where none tells. A call's charge bounds
    each price it paid (charge / input tokens bounds the input price, charge / output tokens
    the output price), the tightest such bound per serving provider holds for it, and the
    highest across the serving providers holds for the model. A call with cached input left out
    of the input price, as caching discounts it."""
    row = await (
        await unit.execute(
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


async def observations(unit: ReadUnit) -> dict[str, dict[str, Any]]:
    """Each provider's latest account error and latest reply, by provider."""
    rows = await (await unit.execute("SELECT * FROM provider_observations")).fetchall()
    return {r["provider"]: r for r in rows}


async def outcomes(unit: ReadUnit, run: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per provider, the run's calls that failed without an answer (provider errors and
    timeouts), and the replies it sent while the run went on, whichever run's calls they
    settled (a dead run's batch, read in this one; reused replies, errors the provider billed or
    rejected unbilled, and evaluation runs' calls, a replay job's included, left out)."""
    rows = await (
        await unit.execute(
            "SELECT provider, sum(failed)::int AS failed, sum(replied)::int AS replied FROM ("
            "SELECT usage->>'via' AS provider, 1 AS failed, 0 AS replied FROM inference_calls "
            "WHERE run_id = %(run)s AND (error LIKE 'provider%%' OR error LIKE 'timeout%%') "
            "UNION ALL SELECT usage->>'via', 0, 1 FROM inference_calls "
            "WHERE responded_at BETWEEN %(since)s AND %(until)s "
            "AND NOT coalesce((usage->>'reused')::boolean, false) "
            "AND NOT coalesce(error LIKE 'provider%%' OR error LIKE 'account%%', false) "
            "AND run_id NOT IN (SELECT id FROM runs WHERE kind = %(eval)s)) seen "
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
