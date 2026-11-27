"""The Work gate's queue (docs/plans/a2-work-gate.md): work items and the processes that serve
them (`workers`).

Submitting is a write like an event's: `submit` runs in the submitter's own unit, so the item
commits or rolls back with the data that calls for it, and its NOTIFY on `CHANNEL` is delivered
when that unit commits. Everything else here is the Work gate's own bookkeeping, in its units:
taking an item with a lease, renewing it, taking expired leases back and settling items.

A change to a running item's lease (renewing it, settling the item) first locks the item's row,
then checks the lease against the clock in a statement of its own: a statement that waited for
the lock would otherwise judge it by the time it started, and keep a lease that ran out while it
waited."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, LiteralString
from uuid import UUID

from psycopg.types.json import Jsonb

from litwatch.core.work import ItemState, Outcome, WorkItem
from litwatch.gates.database import ReadUnit, WriteUnit
from litwatch.store.text import stored

# The channel a submission, and an item's settling, notify the Work gates on; the payload is the
# item's kind.
CHANNEL = "litwatch_work"
# The channel a command's results are notified on (`put_result`), its payload the item's id:
# the command following the item listens on it, the Work gates do not.
RESULTS = "litwatch_work_results"
# The CLI's commands' kinds (`jobs.commands`), as a LIKE pattern: the only items an owner may
# follow or cancel from the CLI, and those tried once (`once`).
COMMANDS = "command:%"
CANCELLED = "cancelled by the owner"
# How a command's item ends when its one attempt could not finish: `serve` stopped, or lost
# the database, while it ran. The command submits it again (`litwatch.cli`).
STOPPED = "serve stopped while it ran"


def once(kind: str) -> bool:
    """Whether items of `kind` get one attempt: a CLI command's. One given back, taken back or
    tried again ends failed (`STOPPED`) instead, and its command submits a fresh one, which
    goes on from what the first committed."""
    return kind.startswith("command:")


_COLUMNS = (
    "id, kind, key, admission, state, not_before, attempts, scope, lease_token, lease_until, "
    "outcome, error, priority, result, cancel_asked"
)
# An item whose key is taken: open, or lasting. As the unique index `work_items_key` says.
_TAKEN = "(lasting OR state <> 'ended')"
# When an item put back to pending is wanted again: at `at` (now when not given), or sooner if a
# resubmission asked for that.
_WANTED = (
    "CASE WHEN resubmitted THEN LEAST(not_before, COALESCE(%(at)s::timestamptz, now())) "
    "ELSE COALESCE(%(at)s::timestamptz, now()) END"
)


def _served(alias: LiteralString) -> LiteralString:
    """Whether item `alias` is one a process serves (`_serving`'s parameters): of its kinds,
    and of a kind it serves for some keys alone, of those keys."""
    return (
        f"{alias}.kind = ANY(%(kinds)s) AND (NOT {alias}.kind = ANY(%(limited)s) "
        f"OR ({alias}.kind, {alias}.key) IN "
        "(SELECT * FROM unnest(%(limited_kinds)s::text[], %(limited_keys)s::text[])))"
    )


def _serving(kinds: list[str], keys: Mapping[str, Collection[str]] | None) -> dict[str, Any]:
    """The parameters of `_served` for a process serving `kinds`, and of a kind in `keys` only
    those keys (an integration's deliveries, which only a process that loads it can run)."""
    limited = keys or {}
    pairs = [(kind, key) for kind in sorted(limited) for key in sorted(limited[kind])]
    return {
        "kinds": kinds,
        "limited": sorted(limited),
        "limited_kinds": [kind for kind, _ in pairs],
        "limited_keys": [key for _, key in pairs],
    }


def _item(row: dict[str, Any]) -> WorkItem:
    return WorkItem(
        id=row["id"],
        kind=row["kind"],
        key=row["key"],
        admission=row["admission"],
        state=ItemState(row["state"]),
        not_before=row["not_before"],
        attempts=row["attempts"],
        scope=row["scope"],
        lease_token=row["lease_token"],
        lease_until=row["lease_until"],
        outcome=Outcome(row["outcome"]) if row["outcome"] else None,
        error=row["error"],
        priority=row["priority"],
        result=row["result"],
        cancel_asked=row["cancel_asked"],
    )


async def _notify(unit: WriteUnit, kind: str) -> None:
    await unit.execute("SELECT pg_notify(%s, %s)", (CHANNEL, kind))


async def submit(
    unit: WriteUnit,
    kind: str,
    key: str,
    *,
    scope: dict[str, Any] | None = None,
    not_before: datetime | None = None,
    admission: str | None = None,
    lasting: bool = False,
    priority: int = 0,
    _ensure: bool = False,
) -> WorkItem:
    """Submit the work `key` of `kind`, to run no earlier than `not_before` (now when not
    given), admitted one at a time per `admission` (its kind when not given), in line among the
    items due by `priority`, the lowest first (`take`). Returns the item.

    A key is the work within its kind (a pass's id, an integration's name). A submission whose
    key is still open is the same item, and its scope stays: a pending one is brought forward
    to `not_before` when that is sooner, and up to `priority` when that is more urgent; a
    running one is marked as resubmitted, wanted again at `not_before` (the sooner of two such
    submissions, and the more urgent priority), so work asked for while it ran is not lost:
    when that run ends, a fresh item takes the key (`end`). A `lasting` key, a scheduled
    occurrence's (the fire time), holds for good: once submitted, it returns its item, pending,
    running or ended, unchanged, and never runs it again.

    The open item's row is locked `FOR NO KEY UPDATE` before its state is read, so a claim or a
    settling of it in flight is waited for and the item returned is as it stands. That lock
    does not wait for a unit fenced on the item, which holds it `FOR KEY SHARE`: the item's own
    unit may submit its key, as a delivery arranging its next turn does, and a submitter holding
    a paper lock that unit waits for does not deadlock with it."""
    params = {
        "kind": kind,
        "key": key,
        "scope": Jsonb({} if scope is None else scope),
        "at": not_before,
        "admission": kind if admission is None else admission,
        "lasting": lasting,
        "priority": priority,
    }
    # A key found free may be taken by a concurrent insert before ours, or the item found may end
    # and hand its key to a successor while we wait for its row: either way it is looked up
    # again, until a row is locked or ours is inserted. Each turn follows a commit elsewhere.
    while True:
        row = await (
            await unit.execute(
                f"SELECT {_COLUMNS}, lasting FROM work_items "
                f"WHERE kind = %(kind)s AND key = %(key)s AND {_TAKEN} FOR NO KEY UPDATE",
                params,
            )
        ).fetchone()
        if row is None:
            inserted = await (
                await unit.execute(
                    "INSERT INTO work_items (kind, key, scope, not_before, admission, lasting, "
                    "priority) VALUES (%(kind)s, %(key)s, %(scope)s, "
                    "COALESCE(%(at)s::timestamptz, now()), %(admission)s, %(lasting)s, "
                    "%(priority)s) "
                    f"ON CONFLICT (kind, key) WHERE {_TAKEN} DO NOTHING RETURNING {_COLUMNS}",
                    params,
                )
            ).fetchone()
            if inserted is None:
                continue
            await _notify(unit, kind)
            return _item(inserted)
        if row["state"] == "ended" or row["lasting"]:
            return _item(row)
        if _ensure:
            if row["priority"] <= priority:
                return _item(row)
            urgent = await (
                await unit.execute(
                    f"UPDATE work_items SET priority = %s WHERE id = %s RETURNING {_COLUMNS}",
                    (priority, row["id"]),
                )
            ).fetchone()
            assert urgent is not None  # locked above
            if urgent["state"] == "pending":
                await _notify(unit, kind)
            return _item(urgent)
        if row["state"] == "pending":
            sooner = await (
                await unit.execute(
                    "UPDATE work_items SET "
                    "not_before = LEAST(not_before, COALESCE(%(at)s::timestamptz, now())), "
                    "priority = LEAST(priority, %(priority)s) WHERE id = %(id)s "
                    "AND (not_before > COALESCE(%(at)s::timestamptz, now()) "
                    "OR priority > %(priority)s) "
                    f"RETURNING {_COLUMNS}",
                    {"id": row["id"], "at": not_before, "priority": priority},
                )
            ).fetchone()
            if sooner is None:
                return _item(row)
            await _notify(unit, kind)
            return _item(sooner)
        marked = await (
            await unit.execute(
                f"UPDATE work_items SET not_before = {_WANTED}, resubmitted = true, "
                "priority = LEAST(priority, %(priority)s) "
                f"WHERE id = %(id)s RETURNING {_COLUMNS}",
                {"id": row["id"], "at": not_before, "priority": priority},
            )
        ).fetchone()
        assert marked is not None  # locked above
        return _item(marked)


async def ensure(
    unit: WriteUnit,
    kind: str,
    key: str,
    *,
    not_before: datetime | None = None,
    admission: str | None = None,
    priority: int = 0,
) -> WorkItem:
    """Make sure the work `key` of `kind` has an open item: submit one when it has none, as
    `submit` would, and otherwise keep the open item's time, state and whether it was
    resubmitted, raising only its priority to `priority` when that is more urgent, as a
    resubmission does. What a process that starts serving a kind runs for the work it finds
    open (a pass's item, `store.fulltext.submit_open`): finding an item is no reason to run it
    sooner, nor again.

    What it reconciles after a rollback to the image before the gate and an upgrade again
    (docs/plans/a2-work-gate.md, "Rollback is a handoff of live work"): work the old image
    queued has no item, and gets one here; work it made more urgent (an automatic Deep pass the
    owner requested, whose priority the old image raised on the pass alone) has its item's
    priority raised to match, so it does not wait behind automatic items. An item it keeps needs
    nothing more: a running one, its `serve` stopped, is taken back once its lease lapses
    (`take`); a pending one waits no longer than the claim, the retry or the pause it was set to
    wait for, then decides afresh (finding its pass done, say, if the old image worked it)."""
    return await submit(
        unit,
        kind,
        key,
        not_before=not_before,
        admission=admission,
        priority=priority,
        _ensure=True,
    )


async def forget_resubmission(unit: WriteUnit, item: WorkItem) -> None:
    """Forget the submissions of the running item's key made while it ran, its row locked to
    the end of the unit, which is the item's own (fenced on it): a Worker that reads what
    stands after this, and submits its key in the same unit if it wants another run, alone
    decides when that run is (a delivery arranging its next turn). A submission that commits
    after this unit is kept."""
    await unit.execute(
        "UPDATE work_items SET resubmitted = false WHERE id = %s AND state = 'running'",
        (item.id,),
    )


async def end_unserved(unit: WriteUnit, kind: str, keys: Collection[str], error: str) -> None:
    """End failed, with `error`, the open items of `kind` whose key is none of `keys`: work no
    process will take (a delivery for an integration no longer configured, as `serve` starts).
    One running is ended whatever its lease, its rows locked in key order, as submitters lock
    them; none is submitted again."""
    await unit.execute(
        "UPDATE work_items SET state = 'ended', outcome = 'failed', ended_at = now(), "
        "error = %(error)s, lease_token = NULL, lease_until = NULL, resubmitted = false "
        "WHERE id IN (SELECT id FROM work_items WHERE kind = %(kind)s AND state <> 'ended' "
        "AND key <> ALL(%(keys)s) ORDER BY key FOR UPDATE)",
        {"kind": kind, "keys": list(keys), "error": stored(error)},
    )


async def done_elsewhere(
    unit: WriteUnit, kind: str, key: str, *, besides: int | None = None
) -> None:
    """The work `key` of `kind` was done elsewhere (a pass a nightly worked inline): its pending
    item ends done, with nothing left to do, and a running one other than `besides` (the
    caller's own item) is wanted again at once, so a Worker that is about to have it wait looks
    again first, and finds the work done. In the unit that did the work.

    Its row is locked as `submit` locks it, and its state read after, in a statement of its
    own: a take or a settling in flight is waited for. One found ended meanwhile may have handed
    its key to a successor, which is looked up again."""
    while True:
        found = await (
            await unit.execute(
                f"SELECT id FROM work_items WHERE kind = %s AND key = %s AND {_TAKEN} "
                "AND NOT lasting",
                (kind, key),
            )
        ).fetchone()
        if found is None or found["id"] == besides:
            return
        await unit.execute(
            "SELECT 1 FROM work_items WHERE id = %s FOR NO KEY UPDATE", (found["id"],)
        )
        row = await (
            await unit.execute("SELECT state FROM work_items WHERE id = %s", (found["id"],))
        ).fetchone()
        assert row is not None  # items are never deleted
        if row["state"] == "pending":
            await unit.execute(
                "UPDATE work_items SET state = 'ended', outcome = 'done', ended_at = now() "
                "WHERE id = %s",
                (found["id"],),
            )
            return
        if row["state"] == "running":
            await unit.execute(
                "UPDATE work_items SET resubmitted = true, not_before = now() WHERE id = %s",
                (found["id"],),
            )
            return


async def take_back(
    unit: WriteUnit, kinds: list[str], keys: Mapping[str, Collection[str]] | None = None
) -> list[WorkItem]:
    """Put the running items of `kinds` (and of a kind in `keys`, only those keys) whose lease
    expired back to pending, to be taken again at once in the place in line they had (a
    resubmission's later wish included): their worker died or lost the database. One whose row
    a unit holds (one fenced on it, or a settling) is left for a later call. Returns those taken
    back.

    A command's item whose lease expired ends failed instead, whatever admission it holds
    (`_retire_commands`): it is not tried again."""
    params = _serving(kinds, keys)
    await _retire_commands(unit)
    rows = await (
        await unit.execute(
            "UPDATE work_items SET state = 'pending', lease_token = NULL, lease_until = NULL, "
            "not_before = LEAST(not_before, now()), resubmitted = false "
            f"WHERE id IN (SELECT id FROM work_items w WHERE {_served('w')} "
            "AND w.kind NOT LIKE %(commands)s "
            "AND state = 'running' AND lease_until < now() FOR UPDATE SKIP LOCKED) "
            f"RETURNING {_COLUMNS}",
            params | {"commands": COMMANDS},
        )
    ).fetchall()
    return [_item(r) for r in rows]


async def _retire_commands(unit: WriteUnit) -> None:
    """End failed the CLI commands' items (`once`) whose lease expired, which no process runs
    again: their `serve` is gone (`STOPPED`, or `CANCELLED` when the owner's cancel was asked
    first). Each holds its admission (`deep`, `publications`) until then."""
    retired = await (
        await unit.execute(
            "UPDATE work_items SET state = 'ended', outcome = 'failed', ended_at = now(), "
            "error = CASE WHEN cancel_asked IS NOT NULL THEN %(cancelled)s ELSE %(stopped)s END, "
            "lease_token = NULL, lease_until = NULL, resubmitted = false "
            "WHERE id IN (SELECT id FROM work_items c WHERE c.kind LIKE %(commands)s "
            "AND c.state = 'running' AND c.lease_until < now() FOR UPDATE SKIP LOCKED) "
            "RETURNING kind",
            {"commands": COMMANDS, "stopped": STOPPED, "cancelled": CANCELLED},
        )
    ).fetchall()
    for kind in sorted({r["kind"] for r in retired}):
        await _notify(unit, kind)  # their admissions are free, their commands are told


async def take(
    unit: WriteUnit,
    kinds: list[str],
    lease: timedelta,
    keys: Mapping[str, Collection[str]] | None = None,
) -> WorkItem | None:
    """Claim the pending item of `kinds` (and of a kind in `keys`, only those keys) that is due,
    of the lowest `priority`, then the one that has waited longest past its `not_before`, whose
    admission has no item running. None when there is none. Two processes claiming for
    one admission at once: the later waits for the earlier's commit, then fails on
    `work_items_admitted`. The lease it is given is stamped by `stamp`, in the same unit: this
    statement may have waited for that admission."""
    row = await (
        await unit.execute(
            f"""
            WITH next AS (
                SELECT id FROM work_items w
                WHERE {_served("w")} AND state = 'pending' AND not_before <= now()
                  AND NOT EXISTS (SELECT 1 FROM work_items r
                                  WHERE r.admission = w.admission AND r.state = 'running')
                ORDER BY priority, not_before, id LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            UPDATE work_items SET state = 'running', lease_token = gen_random_uuid(),
                                  lease_until = now() + %(lease)s,
                                  attempts = attempts + 1, resubmitted = false
            WHERE id = (SELECT id FROM next) RETURNING {_COLUMNS}
            """,
            {**_serving(kinds, keys), "lease": lease},
        )
    ).fetchone()
    return _item(row) if row else None


async def stamp(unit: WriteUnit, item: WorkItem, lease: timedelta) -> WorkItem:
    """Start the lease of an item `take` claimed in this unit: `lease` from the clock now, which
    is when it started."""
    row = await (
        await unit.execute(
            f"UPDATE work_items SET started_at = c.at, lease_until = c.at + %s "
            f"FROM (SELECT clock_timestamp() AS at) c WHERE id = %s RETURNING {_COLUMNS}",
            (lease, item.id),
        )
    ).fetchone()
    assert row is not None  # claimed in this unit
    return _item(row)


async def _held(unit: WriteUnit, item_id: int, token: UUID, *, live: bool) -> dict[str, Any] | None:
    """Lock the item's row and say whether `token` holds it, its lease unexpired with `live`:
    when it does, the clock's reading (`at`) and whether the item was resubmitted; None when it
    does not."""
    await unit.execute("SELECT 1 FROM work_items WHERE id = %s FOR UPDATE", (item_id,))
    row = await (
        await unit.execute(
            "SELECT c.at, w.resubmitted, w.cancel_asked, "
            "COALESCE(w.lease_token = %s AND (NOT %s OR w.lease_until > c.at), false) AS held "
            "FROM work_items w, (SELECT clock_timestamp() AS at) c WHERE w.id = %s",
            (token, live, item_id),
        )
    ).fetchone()
    return row if row and row["held"] else None


async def renew(unit: WriteUnit, item_id: int, token: UUID, lease: timedelta) -> bool:
    """Renew the lease `token` on the item for `lease` from now. False when it is no longer
    held: it expired or was taken back. Waits for a unit fenced on the item to end."""
    held = await _held(unit, item_id, token, live=True)
    if held is None:
        return False
    await unit.execute(
        "UPDATE work_items SET lease_until = %s::timestamptz + %s WHERE id = %s",
        (held["at"], lease, item_id),
    )
    return True


async def end(
    unit: WriteUnit,
    item: WorkItem,
    outcome: Outcome,
    *,
    error: str | None = None,
    cost: Decimal | None = None,
    result: Any = None,
    live: bool = True,
) -> bool:
    """End the item held under its lease, with `outcome` (and `result`, a command's outcome,
    which its command shows). The lease must be unexpired but without `live`, which is for an
    item whose Worker has stopped (a command's, which the gate ends rather than give back as it
    stops). An item ended as stopped (`STOPPED`) whose owner's cancel was asked first ends
    `CANCELLED`, so its command does not submit it again. One resubmitted while it ran ends as
    itself, its record kept, and a fresh pending item takes its key, with its scope and
    admission, wanted when the resubmission asked. False when the lease is no longer held: the
    item is not this worker's to settle."""
    assert item.lease_token is not None  # a taken item has its lease
    held = await _held(unit, item.id, item.lease_token, live=live)
    if held is None:
        return False
    if held["cancel_asked"] is not None and (error or "").startswith(STOPPED):
        error = CANCELLED  # the owner's cancel, committed first, is what ended it
    kept = None if result is None else Jsonb(result)
    await unit.execute(
        "UPDATE work_items SET state = 'ended', outcome = %s, error = %s, cost = %s, "
        "result = %s, ended_at = %s, lease_token = NULL, lease_until = NULL, "
        "resubmitted = false WHERE id = %s",
        (outcome, _stored(error), cost, kept, held["at"], item.id),
    )
    if held["resubmitted"]:
        await unit.execute(
            "INSERT INTO work_items (kind, key, scope, admission, not_before, priority) "
            "SELECT kind, key, scope, admission, not_before, priority FROM work_items "
            "WHERE id = %s",
            (item.id,),
        )
    await _notify(unit, item.kind)
    return True


def _stored(error: str | None) -> str | None:
    """An error as stored: at most 2000 characters, without NULs, which text columns refuse."""
    return None if error is None else stored(error)


async def again(
    unit: WriteUnit, item: WorkItem, not_before: datetime | None = None, *, live: bool = True
) -> bool:
    """Put the item, held under its lease, back to pending, to run again no earlier than
    `not_before` (sooner if it was resubmitted meanwhile); when not given, at once, in the place
    in line it had (its `not_before`, as `take_back` keeps it). The lease must be unexpired but
    without `live`, which is for an item whose Worker has stopped (the gate giving it back as it
    stops). A command's item whose owner asked to cancel it ends `CANCELLED` instead, as it
    would have at its next claim. False when the lease is no longer held."""
    assert item.lease_token is not None  # a taken item has its lease
    held = await _held(unit, item.id, item.lease_token, live=live)
    if held is None:
        return False
    if held["cancel_asked"] is not None and once(item.kind):
        await unit.execute(
            "UPDATE work_items SET state = 'ended', outcome = 'failed', error = %s, "
            "ended_at = %s, lease_token = NULL, lease_until = NULL, resubmitted = false "
            "WHERE id = %s",
            (CANCELLED, held["at"], item.id),
        )
        await _notify(unit, item.kind)
        return True
    wanted = "LEAST(not_before, now())" if not_before is None else _WANTED
    await unit.execute(
        f"UPDATE work_items SET state = 'pending', lease_token = NULL, lease_until = NULL, "
        f"not_before = {wanted}, resubmitted = false WHERE id = %(id)s",
        {"at": not_before, "id": item.id},
    )
    await _notify(unit, item.kind)
    return True


async def wait_seconds(
    unit: ReadUnit, kinds: list[str], keys: Mapping[str, Collection[str]] | None = None
) -> float | None:
    """Seconds until something of `kinds` (and of a kind in `keys`, only those keys) changes
    without a notification: a pending item whose admission is free comes to its `not_before`,
    or a running one's lease expires. None when nothing waits; 0 or less when it is due
    already."""
    row = await (
        await unit.execute(
            "SELECT EXTRACT(EPOCH FROM min(at) - now())::float AS seconds FROM ("
            "SELECT w.not_before AS at FROM work_items w "
            f"WHERE {_served('w')} AND w.state = 'pending' "
            "AND NOT EXISTS (SELECT 1 FROM work_items r "
            "WHERE r.admission = w.admission AND r.state = 'running') "
            "UNION ALL SELECT l.lease_until FROM work_items l "
            f"WHERE {_served('l')} AND l.state = 'running') due",
            _serving(kinds, keys),
        )
    ).fetchone()
    assert row is not None  # an aggregate has its row
    return row["seconds"]


async def link_run(unit: WriteUnit, item: WorkItem, run_id: int) -> None:
    """Record that the running item works the run `run_id` (a job's chunk, on the job's run):
    the run is alive by this claim of the item, while its lease is (`store.runs.ALIVE`), and no
    longer by an earlier chunk's. In the item's own unit."""
    await unit.execute("UPDATE work_items SET run_id = %s WHERE id = %s", (run_id, item.id))
    await unit.execute("UPDATE runs SET lease_token = %s WHERE id = %s", (item.lease_token, run_id))


async def submitted(unit: ReadUnit, kind: str, key: str) -> bool:
    """Whether an item of `kind` and `key` was ever submitted: for a `lasting` key, whether its
    one item exists."""
    row = await (
        await unit.execute(
            "SELECT 1 FROM work_items WHERE kind = %s AND key = %s LIMIT 1", (kind, key)
        )
    ).fetchone()
    return row is not None


async def bring_forward(unit: WriteUnit, kind: str, key: str, at: datetime) -> None:
    """Bring the pending item of `kind` and `key` forward to `at`, where that is sooner: work
    that waited on something now over (a lock let go of) goes when it is due."""
    moved = await unit.execute(
        "UPDATE work_items SET not_before = %s WHERE kind = %s AND key = %s "
        "AND state = 'pending' AND not_before > %s",
        (at, kind, key, at),
    )
    if moved.rowcount:
        await _notify(unit, kind)


async def get(unit: ReadUnit, item_id: int) -> WorkItem | None:
    row = await (
        await unit.execute(f"SELECT {_COLUMNS} FROM work_items WHERE id = %s", (item_id,))
    ).fetchone()
    return _item(row) if row else None


async def find(unit: ReadUnit, kind: str, key: str) -> WorkItem | None:
    """The latest item of `kind` and `key`, if any: a command's, found by the key it chose
    before it knew whether its submission committed."""
    row = await (
        await unit.execute(
            f"SELECT {_COLUMNS} FROM work_items WHERE kind = %s AND key = %s "
            "ORDER BY id DESC LIMIT 1",
            (kind, key),
        )
    ).fetchone()
    return _item(row) if row else None


async def cancel(unit: WriteUnit, item_id: int) -> WorkItem | None:
    """The owner cancels the CLI command's item `item_id` (at Ctrl-C), or None when it is no
    command's. One pending ends failed at once (`CANCELLED`); one running keeps its lease and
    has the cancel asked (`cancel_asked`), notified: the supervisor owning it stops its Worker,
    lets its cleanup finish under its claim, and only then ends it (`gates.work`). Returns the
    item as it stands after."""
    row = await (
        await unit.execute(
            f"SELECT {_COLUMNS} FROM work_items WHERE id = %s AND kind LIKE %s FOR UPDATE",
            (item_id, COMMANDS),
        )
    ).fetchone()
    if row is None or row["state"] == ItemState.ENDED:
        return _item(row) if row else None
    if row["state"] == ItemState.PENDING:
        query = (
            "UPDATE work_items SET state = 'ended', outcome = 'failed', error = %s, "
            "ended_at = now(), resubmitted = false, cancel_asked = now() "
            f"WHERE id = %s AND kind LIKE %s RETURNING {_COLUMNS}"
        )
        params: tuple[Any, ...] = (CANCELLED, item_id, COMMANDS)
    else:
        query = (
            "UPDATE work_items SET cancel_asked = coalesce(cancel_asked, now()) "
            f"WHERE id = %s AND kind LIKE %s RETURNING {_COLUMNS}"
        )
        params = (item_id, COMMANDS)
    found = await (await unit.execute(query, params)).fetchone()
    assert found is not None  # the row locked above
    await _notify(unit, found["kind"])
    return _item(found)


async def asked_to_cancel(unit: ReadUnit, item_ids: Collection[int]) -> list[int]:
    """Which of the running items `item_ids` their owner asked to cancel."""
    rows = await (
        await unit.execute(
            "SELECT id FROM work_items WHERE id = ANY(%s) AND cancel_asked IS NOT NULL",
            (list(item_ids),),
        )
    ).fetchall()
    return [r["id"] for r in rows]


async def put_result(unit: WriteUnit, item: WorkItem, body: dict[str, Any]) -> None:
    """Add `body` to the item's results, in the item's own unit (fenced on its lease, so a
    result of a lost lease never commits), notifying its follower on `RESULTS`."""
    await unit.execute(
        "INSERT INTO work_results (work_id, body) VALUES (%s, %s)", (item.id, Jsonb(body))
    )
    await unit.execute("SELECT pg_notify(%s, %s)", (RESULTS, str(item.id)))


async def results(unit: ReadUnit, item_id: int, after: int = 0) -> list[tuple[int, dict[str, Any]]]:
    """The item's results after the one of id `after`, in order, with their ids."""
    rows = await (
        await unit.execute(
            "SELECT id, body FROM work_results WHERE work_id = %s AND id > %s ORDER BY id",
            (item_id, after),
        )
    ).fetchall()
    return [(r["id"], r["body"]) for r in rows]


# -- the processes that serve work items -----------------------------------------------------------


async def register(unit: WriteUnit, name: str, kinds: list[str], *, supersede: bool = False) -> int:
    """Record a process serving `kinds` as `name`; returns its row's id. With `supersede`
    (`serve`'s), every earlier row goes in the same unit: under D4 one `serve` serves, and one
    killed leaves its row, which would advertise what it served until its heartbeat aged a
    lease, whatever host name its replacement runs under."""
    if supersede:
        await unit.execute("DELETE FROM workers")
    row = await (
        await unit.execute(
            "INSERT INTO workers (name, kinds) VALUES (%s, %s) RETURNING id", (name, kinds)
        )
    ).fetchone()
    assert row is not None  # the insert returns it
    return row["id"]


async def beat(unit: WriteUnit, worker_id: int) -> None:
    await unit.execute("UPDATE workers SET heartbeat_at = now() WHERE id = %s", (worker_id,))


async def retire(unit: WriteUnit, worker_id: int, wait: timedelta) -> None:
    """Delete this process's `workers` row, waiting at most `wait` for a submission holding it
    (`served_here`): past that the row's lock is not had (`LockNotAvailable`), and it stays
    until its heartbeat is a lease old."""
    await unit.execute(
        "SELECT set_config('lock_timeout', %s, true)", (f"{wait.total_seconds() * 1000:.0f}ms",)
    )
    await unit.execute("DELETE FROM workers WHERE id = %s", (worker_id,))


async def serving(unit: ReadUnit, kind: str, within: timedelta) -> bool:
    """Whether a process serves `kind`: its `workers` row lists it and beat within `within`
    (a lease: a row older than that is a process that died without retiring it)."""
    row = await (
        await unit.execute(
            "SELECT EXISTS (SELECT 1 FROM workers WHERE %s = ANY(kinds) "
            "AND heartbeat_at > now() - %s) AS serving",
            (kind, within),
        )
    ).fetchone()
    assert row is not None  # a SELECT of a value has its row
    return row["serving"]


async def served_here(unit: WriteUnit, kind: str, within: timedelta) -> bool:
    """`serving`, in the unit that submits a command's item, the rows found locked to its end:
    a `serve` retiring its row (`retire`) waits for the submission, which its gate then leaves
    pending for the next `serve`, or the submission sees the row gone and is refused. A blank
    database, never migrated, has no `serve` either."""
    if not await migrated(unit):
        return False
    rows = await (
        await unit.execute(
            "SELECT id FROM workers WHERE %s = ANY(kinds) AND heartbeat_at > now() - %s FOR SHARE",
            (kind, within),
        )
    ).fetchall()
    return bool(rows)


async def migrated(unit: ReadUnit) -> bool:
    """Whether the database has the work queue at all: a blank one, never migrated (no `serve`
    has started on it), has none."""
    row = await (
        await unit.execute("SELECT to_regclass('workers') IS NOT NULL AS found")
    ).fetchone()
    assert row is not None  # a SELECT of a value has its row
    return row["found"]
