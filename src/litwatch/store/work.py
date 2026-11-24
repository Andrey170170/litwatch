"""The Work gate's queue (docs/plans/a2-work-gate.md): work items and the processes that serve
them (`workers`).

Submitting is a write like an event's: `submit` runs in the submitter's own unit, so the item
commits or rolls back with the data that calls for it, and its NOTIFY on `CHANNEL` is delivered
when that unit commits. Everything else here is the Work gate's own bookkeeping, in its units:
taking an item with a lease, renewing it, taking expired leases back and ending items."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from litwatch.core.work import ItemState, Outcome, WorkItem
from litwatch.gates.database import ReadUnit, WriteUnit

# The channel a submission, and an item's end, notify the Work gates on; the payload is the
# item's kind.
CHANNEL = "litwatch_work"

_COLUMNS = (
    "id, kind, key, admission, state, not_before, attempts, scope, lease_token, lease_until, "
    "outcome, error"
)
# An item whose key is taken: open, or lasting. As the unique index `work_items_key` says.
_TAKEN = "(lasting OR state <> 'ended')"


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
) -> WorkItem:
    """Submit the work `key` of `kind`, to run no earlier than `not_before` (now when not
    given), admitted one at a time per `admission` (its kind when not given). Returns the item.

    A key is the work within its kind (a pass's id, an integration's name). While an item with
    the key is open (pending or running) it is the one returned, and a pending one is brought
    forward to `not_before` when that is sooner; its scope stays. A `lasting` key, a scheduled
    occurrence's (the fire time), holds for good: once submitted, it returns its item ended or
    not, so a fire runs once however many processes submit it."""
    params = {
        "kind": kind,
        "key": key,
        "scope": Jsonb({} if scope is None else scope),
        "not_before": not_before,
        "admission": kind if admission is None else admission,
        "lasting": lasting,
    }
    # The insert yields to an item holding the key; one that ends between the insert and the
    # read leaves the key free again, so the insert is tried once more.
    for _ in range(3):
        row = await (
            await unit.execute(
                f"INSERT INTO work_items (kind, key, scope, not_before, admission, lasting) "
                f"VALUES (%(kind)s, %(key)s, %(scope)s, "
                f"COALESCE(%(not_before)s::timestamptz, now()), %(admission)s, %(lasting)s) "
                f"ON CONFLICT (kind, key) WHERE {_TAKEN} DO NOTHING RETURNING {_COLUMNS}",
                params,
            )
        ).fetchone()
        if row is not None:
            await _notify(unit, kind)
            return _item(row)
        row = await (
            await unit.execute(
                f"SELECT {_COLUMNS} FROM work_items "
                f"WHERE kind = %(kind)s AND key = %(key)s AND {_TAKEN}",
                params,
            )
        ).fetchone()
        if row is None:
            continue
        if row["state"] != "pending":
            return _item(row)
        sooner = await (
            await unit.execute(
                "UPDATE work_items SET not_before = COALESCE(%(not_before)s::timestamptz, now()) "
                "WHERE id = %(id)s AND state = 'pending' "
                "AND not_before > COALESCE(%(not_before)s::timestamptz, now()) "
                f"RETURNING {_COLUMNS}",
                {"id": row["id"], "not_before": not_before},
            )
        ).fetchone()
        if sooner is None:  # not sooner, or leased meanwhile
            return _item(row)
        await _notify(unit, kind)
        return _item(sooner)
    raise RuntimeError(f"work item {kind} {key!r} could be neither submitted nor found")


async def take_back(unit: WriteUnit, kinds: list[str]) -> list[WorkItem]:
    """Put the running items of `kinds` whose lease expired back to pending, to be taken again:
    their worker died or lost the database. Waits for a unit fenced on one to end. Returns
    them."""
    rows = await (
        await unit.execute(
            "UPDATE work_items SET state = 'pending', lease_token = NULL, lease_until = NULL "
            "WHERE kind = ANY(%s) AND state = 'running' AND lease_until < now() "
            f"RETURNING {_COLUMNS}",
            (kinds,),
        )
    ).fetchall()
    return [_item(r) for r in rows]


async def take(unit: WriteUnit, kinds: list[str], lease: timedelta) -> WorkItem | None:
    """Lease the pending item of `kinds` that has waited longest past its `not_before` and
    whose admission has no item running, for `lease`. None when there is none. Two processes
    taking for one admission at once: the later fails on `work_items_admitted`."""
    row = await (
        await unit.execute(
            f"""
            WITH next AS (
                SELECT id FROM work_items w
                WHERE kind = ANY(%(kinds)s) AND state = 'pending' AND not_before <= now()
                  AND NOT EXISTS (SELECT 1 FROM work_items r
                                  WHERE r.admission = w.admission AND r.state = 'running')
                ORDER BY not_before, id LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            UPDATE work_items SET state = 'running', lease_token = gen_random_uuid(),
                                  lease_until = now() + %(lease)s, started_at = now(),
                                  attempts = attempts + 1
            WHERE id = (SELECT id FROM next) RETURNING {_COLUMNS}
            """,
            {"kinds": kinds, "lease": lease},
        )
    ).fetchone()
    return _item(row) if row else None


async def renew(unit: WriteUnit, item_id: int, token: UUID, lease: timedelta) -> bool:
    """Renew the lease `token` on the item for `lease` from now. False when it is no longer
    held: it expired or was taken back. Waits for a unit fenced on the item to end."""
    cur = await unit.execute(
        "UPDATE work_items SET lease_until = now() + %s "
        "WHERE id = %s AND lease_token = %s AND lease_until > now()",
        (lease, item_id, token),
    )
    return cur.rowcount == 1


async def end(
    unit: WriteUnit,
    item: WorkItem,
    outcome: Outcome,
    *,
    error: str | None = None,
    cost: Decimal | None = None,
) -> bool:
    """End the item, held under its lease token, with `outcome`. False when the lease was taken
    back meanwhile: the item is no longer this worker's to end."""
    cur = await unit.execute(
        "UPDATE work_items SET state = 'ended', outcome = %s, error = %s, cost = %s, "
        "ended_at = now(), lease_token = NULL, lease_until = NULL "
        "WHERE id = %s AND lease_token = %s",
        (outcome, error[:2000] if error is not None else None, cost, item.id, item.lease_token),
    )
    if cur.rowcount != 1:
        return False
    await _notify(unit, item.kind)
    return True


async def again(unit: WriteUnit, item: WorkItem, not_before: datetime | None = None) -> bool:
    """Give the item, held under its lease token, back to pending, to run again no earlier
    than `not_before` (now when not given). False when the lease was taken back meanwhile."""
    cur = await unit.execute(
        "UPDATE work_items SET state = 'pending', lease_token = NULL, lease_until = NULL, "
        "not_before = COALESCE(%s::timestamptz, now()) WHERE id = %s AND lease_token = %s",
        (not_before, item.id, item.lease_token),
    )
    if cur.rowcount != 1:
        return False
    await _notify(unit, item.kind)
    return True


async def wait_seconds(unit: ReadUnit, kinds: list[str]) -> float | None:
    """Seconds until something of `kinds` changes without a notification: a pending item's
    `not_before` comes, or a running one's lease expires. None when nothing waits; 0 or less
    when it is already due."""
    row = await (
        await unit.execute(
            "SELECT EXTRACT(EPOCH FROM min(CASE state WHEN 'pending' THEN not_before "
            "ELSE lease_until END) - now())::float AS seconds FROM work_items "
            "WHERE kind = ANY(%s) AND state <> 'ended'",
            (kinds,),
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

# A worker's row whose heartbeat is older than this is a process that died without removing it.
_GONE = timedelta(days=1)


async def register(unit: WriteUnit, name: str, kinds: list[str]) -> int:
    """Record a process serving `kinds` as `name`; returns its row's id. Rows left by processes
    long dead go."""
    await unit.execute("DELETE FROM workers WHERE heartbeat_at < now() - %s", (_GONE,))
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
