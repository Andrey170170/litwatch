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

# The channel a submission, and an item's settling, notify the Work gates on; the payload is the
# item's kind.
CHANNEL = "litwatch_work"

_COLUMNS = (
    "id, kind, key, admission, state, not_before, attempts, scope, lease_token, lease_until, "
    "outcome, error"
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
    keep: bool = False,
) -> WorkItem:
    """Submit the work `key` of `kind`, to run no earlier than `not_before` (now when not
    given), admitted one at a time per `admission` (its kind when not given). Returns the item.

    A key is the work within its kind (a pass's id, an integration's name). A submission whose
    key is still open is the same item, and its scope stays: a pending one is brought forward
    to `not_before` when that is sooner; a running one is marked as resubmitted, wanted again
    at `not_before` (the sooner of two such submissions), so work asked for while it ran is not
    lost: when that run ends, a fresh item takes the key (`end`). A `lasting` key, a scheduled
    occurrence's (the fire time), holds for good: once submitted, it returns its item, pending,
    running or ended, unchanged, and never runs it again. With `keep`, a pending item keeps its
    `not_before`: an event wakes a delivery, but never cuts short the backoff it waits out.

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
                    f"INSERT INTO work_items (kind, key, scope, not_before, admission, lasting) "
                    f"VALUES (%(kind)s, %(key)s, %(scope)s, "
                    f"COALESCE(%(at)s::timestamptz, now()), %(admission)s, %(lasting)s) "
                    f"ON CONFLICT (kind, key) WHERE {_TAKEN} DO NOTHING RETURNING {_COLUMNS}",
                    params,
                )
            ).fetchone()
            if inserted is None:
                continue
            await _notify(unit, kind)
            return _item(inserted)
        if row["state"] == "ended" or row["lasting"] or (keep and row["state"] == "pending"):
            return _item(row)
        if row["state"] == "pending":
            sooner = await (
                await unit.execute(
                    "UPDATE work_items SET not_before = COALESCE(%(at)s::timestamptz, now()) "
                    "WHERE id = %(id)s AND not_before > COALESCE(%(at)s::timestamptz, now()) "
                    f"RETURNING {_COLUMNS}",
                    {"id": row["id"], "at": not_before},
                )
            ).fetchone()
            if sooner is None:
                return _item(row)
            await _notify(unit, kind)
            return _item(sooner)
        marked = await (
            await unit.execute(
                f"UPDATE work_items SET not_before = {_WANTED}, resubmitted = true "
                f"WHERE id = %(id)s RETURNING {_COLUMNS}",
                {"id": row["id"], "at": not_before},
            )
        ).fetchone()
        assert marked is not None  # locked above
        return _item(marked)


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


async def take_back(
    unit: WriteUnit, kinds: list[str], keys: Mapping[str, Collection[str]] | None = None
) -> list[WorkItem]:
    """Put the running items of `kinds` (and of a kind in `keys`, only those keys) whose lease
    expired back to pending, to be taken again at once in the place in line they had (a
    resubmission's later wish included): their worker died or lost the database. One whose row
    a unit holds (one fenced on it, or a settling) is left for a later call. Returns those taken
    back."""
    rows = await (
        await unit.execute(
            "UPDATE work_items SET state = 'pending', lease_token = NULL, lease_until = NULL, "
            "not_before = LEAST(not_before, now()), resubmitted = false "
            f"WHERE id IN (SELECT id FROM work_items w WHERE {_served('w')} "
            "AND state = 'running' AND lease_until < now() FOR UPDATE SKIP LOCKED) "
            f"RETURNING {_COLUMNS}",
            _serving(kinds, keys),
        )
    ).fetchall()
    return [_item(r) for r in rows]


async def take(
    unit: WriteUnit,
    kinds: list[str],
    lease: timedelta,
    keys: Mapping[str, Collection[str]] | None = None,
) -> WorkItem | None:
    """Claim the pending item of `kinds` (and of a kind in `keys`, only those keys) that has
    waited longest past its `not_before` and whose admission has no item running. None when
    there is none. Two processes claiming for
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
                ORDER BY not_before, id LIMIT 1 FOR UPDATE SKIP LOCKED
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
            "SELECT c.at, w.resubmitted, "
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
) -> bool:
    """End the item held under its live lease, with `outcome`. One resubmitted while it ran
    ends as itself, its record kept, and a fresh pending item takes its key, with its scope and
    admission, wanted when the resubmission asked. False when the lease is no longer held: the
    item is not this worker's to settle."""
    assert item.lease_token is not None  # a taken item has its lease
    held = await _held(unit, item.id, item.lease_token, live=True)
    if held is None:
        return False
    await unit.execute(
        "UPDATE work_items SET state = 'ended', outcome = %s, error = %s, cost = %s, "
        "ended_at = %s, lease_token = NULL, lease_until = NULL, resubmitted = false "
        "WHERE id = %s",
        (outcome, _stored(error), cost, held["at"], item.id),
    )
    if held["resubmitted"]:
        await unit.execute(
            "INSERT INTO work_items (kind, key, scope, admission, not_before) "
            "SELECT kind, key, scope, admission, not_before FROM work_items WHERE id = %s",
            (item.id,),
        )
    await _notify(unit, item.kind)
    return True


def _stored(error: str | None) -> str | None:
    """An error as stored: at most 2000 characters, without NULs, which text columns refuse."""
    return None if error is None else error.replace("\x00", "\ufffd")[:2000]


async def again(
    unit: WriteUnit, item: WorkItem, not_before: datetime | None = None, *, live: bool = True
) -> bool:
    """Put the item, held under its lease, back to pending, to run again no earlier than
    `not_before` (sooner if it was resubmitted meanwhile); when not given, at once, in the place
    in line it had (its `not_before`, as `take_back` keeps it). The lease must be unexpired but
    without `live`, which is for an item whose Worker has stopped (the gate giving it back as it
    stops). False when the lease is no longer held."""
    assert item.lease_token is not None  # a taken item has its lease
    if await _held(unit, item.id, item.lease_token, live=live) is None:
        return False
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


async def get(unit: ReadUnit, item_id: int) -> WorkItem | None:
    row = await (
        await unit.execute(f"SELECT {_COLUMNS} FROM work_items WHERE id = %s", (item_id,))
    ).fetchone()
    return _item(row) if row else None


# -- the processes that serve work items -----------------------------------------------------------


async def register(unit: WriteUnit, name: str, kinds: list[str]) -> int:
    """Record a process serving `kinds` as `name`; returns its row's id."""
    row = await (
        await unit.execute(
            "INSERT INTO workers (name, kinds) VALUES (%s, %s) RETURNING id", (name, kinds)
        )
    ).fetchone()
    assert row is not None  # the insert returns it
    return row["id"]


async def beat(unit: WriteUnit, worker_id: int) -> None:
    await unit.execute("UPDATE workers SET heartbeat_at = now() WHERE id = %s", (worker_id,))


async def retire(unit: WriteUnit, worker_id: int) -> None:
    await unit.execute("DELETE FROM workers WHERE id = %s", (worker_id,))
