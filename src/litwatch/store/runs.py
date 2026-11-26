"""Run records: one row per execution of a job, with its window, counts, cost and error."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from litwatch.gates.context import current_work
from litwatch.gates.database import ReadUnit, WriteUnit
from litwatch.store.text import stored, stored_json

_JSON = {"counts", "intake"}
_FIELDS = {"fetched", "inserted", "updated", "cost", "error", *_JSON}


# The row of the session a statement runs in, as `pg_stat_activity` shows it (`a`): what the old
# liveness columns (`backend_pid`, `backend_start`) are written from.
SESSION = "pg_stat_activity a WHERE a.pid = pg_backend_pid()"

# Whether run `r` is alive: the claim of the work item it runs in (`runs.lease_token`, the one
# it was opened under, or its job's latest chunk's) is still held, unexpired. A process that
# dies without closing its run (killed, out of memory) stops renewing the lease, so its run is
# over once that lapses: open, not alive; and the item taken again is a new claim, with a token
# of its own, which revives no run of the one before.
ALIVE = (
    "EXISTS (SELECT 1 FROM work_items w WHERE w.lease_token = r.lease_token "
    "AND w.state = 'running' AND w.lease_until > now())"
)


async def start(
    unit: WriteUnit,
    kind: str,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
    job_id: int | None = None,
) -> int:
    """Open a run, alive by the claim of the work item it runs in (its `lease_token`, `ALIVE`).
    It names the work item it was opened in (`work_id`, docs/specs/architecture/a2-work-gate.md,
    "Work records"), which keeps which item ran it when that item later runs another (taken
    back, it opens a run of its own); a run of kind `job` names its job too, and each chunk item
    that works it names the run and moves its claim to that chunk's (`store.jobs.link`). The old
    liveness columns get the session writing it, which ends with the process: until P7 they are
    what the image before the Work gate reads (docs/plans/a2-work-gate.md, "Rollback is a
    handoff of live work")."""
    work = current_work()
    work_id = work.item.id if work is not None else None
    token = work.item.lease_token if work is not None else None
    row = await (
        await unit.execute(
            "INSERT INTO runs (kind, window_from, window_to, job_id, work_id, lease_token, "
            "backend_pid, backend_start) SELECT %s, %s, %s, %s, %s, %s, a.pid, a.backend_start "
            f"FROM {SESSION} RETURNING id",
            (kind, window_from, window_to, job_id, work_id, token),
        )
    ).fetchone()
    return row["id"]


async def latest(unit: ReadUnit, kind: str) -> dict[str, Any] | None:
    """The newest run of `kind`, with `alive`."""
    return await (
        await unit.execute(
            f"SELECT r.*, {ALIVE} AS alive FROM runs r WHERE kind = %s ORDER BY id DESC LIMIT 1",
            (kind,),
        )
    ).fetchone()


async def latest_finished(unit: ReadUnit, kind: str) -> dict[str, Any] | None:
    """The newest run of `kind` that finished, with an error or not. One whose process died has
    no known end, nor its counts."""
    return await (
        await unit.execute(
            "SELECT * FROM runs WHERE kind = %s AND finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (kind,),
        )
    ).fetchone()


async def last_success(unit: ReadUnit, kind: str) -> dict[str, Any] | None:
    """The newest run of `kind` that finished without an error."""
    return await (
        await unit.execute(
            "SELECT * FROM runs WHERE kind = %s AND finished_at IS NOT NULL AND error IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (kind,),
        )
    ).fetchone()


# Whether run `r` served the fires of its kind it started after: one this image ran (in a work
# item, `work_id`) once it closed without an error; one of the image before the Work gate (no
# item) by starting, as that image counted it.
_SERVES = "(r.work_id IS NULL OR (r.finished_at IS NOT NULL AND r.error IS NULL))"


async def last_served(unit: ReadUnit, kind: str) -> datetime | None:
    """When the newest run of `kind` that serves a fire started (`_SERVES`): a fire since is
    due. A run of this image still open serves none yet: its fire is submitted, and its item
    finds out under the run lock, or its kind's admission, whether that run served it
    (`served`)."""
    row = await (
        await unit.execute(
            f"SELECT max(r.started_at) AS at FROM runs r WHERE r.kind = %s AND {_SERVES}",
            (kind,),
        )
    ).fetchone()
    return row["at"]


async def served(unit: ReadUnit, kind: str, fired: datetime) -> bool:
    """Whether a run of `kind` served the fire at `fired`: one started at or after it that serves
    (`_SERVES`): closed without an error (by hand, or the fire's own earlier attempt, its item's
    end lost after it), or the image before's. One cut off (the database lost, its close failed,
    its process or the service stopping) served nothing. Read where no other run of `kind` can
    be under way (under the run lock, or the kind's admission), so a run not closed is a dead
    one."""
    row = await (
        await unit.execute(
            "SELECT EXISTS (SELECT 1 FROM runs r WHERE r.kind = %s AND r.started_at >= %s "
            f"AND {_SERVES}) AS served",
            (kind, fired),
        )
    ).fetchone()
    assert row is not None  # a SELECT of a value has its row
    return row["served"]


async def first_since(unit: ReadUnit, kind: str, since: datetime) -> datetime | None:
    """When the first run of `kind` started at or after `since`."""
    row = await (
        await unit.execute(
            "SELECT min(started_at) AS at FROM runs WHERE kind = %s AND started_at >= %s",
            (kind, since),
        )
    ).fetchone()
    return row["at"]


async def last_harvest_end(unit: ReadUnit, before: int) -> datetime | None:
    """The end of the window of the newest run before run `before` that harvested arXiv in
    full: it finished without an error, fetched papers, and its intake was complete. A read
    from the archive covers new papers only, not every change, so it is no harvest."""
    row = await (
        await unit.execute(
            "SELECT window_to FROM runs WHERE id<%s AND finished_at IS NOT NULL "
            "AND window_to IS NOT NULL AND error IS NULL AND fetched>0 AND (intake IS NULL OR "
            "(intake->>'status'='complete' AND intake->>'source' IS DISTINCT FROM 'archive')) "
            "ORDER BY id DESC LIMIT 1",
            (before,),
        )
    ).fetchone()
    return row["window_to"] if row else None


async def update(unit: WriteUnit, run_id: int, **fields: Any) -> None:
    """Set any of fetched, inserted, updated, cost, error, counts, intake on a run that is
    still going."""
    await _set(unit, run_id, [], fields)


async def finish(unit: WriteUnit, run_id: int, **fields: Any) -> None:
    """Close the run, setting any of the fields `update` takes. Its end is the clock's as this
    is written, after the locks its unit waited for: a nightly's, compared with when a job was
    put on tonight's run under the same lock (`store.jobs.release`), says which went first."""
    await _set(unit, run_id, [sql.SQL("finished_at = clock_timestamp()")], fields)


async def _set(
    unit: WriteUnit, run_id: int, sets: list[sql.Composable], fields: dict[str, Any]
) -> None:
    if unknown := set(fields) - _FIELDS:
        raise ValueError(f"unknown run fields: {sorted(unknown)}")
    if not sets and not fields:
        return
    values = []
    for name, value in fields.items():
        sets.append(sql.SQL("{} = %s").format(sql.Identifier(name)))
        if name == "error" and value is not None:  # text from outside: an exception's message
            value = stored(value)
        values.append(Jsonb(stored_json(value)) if name in _JSON and value is not None else value)
    await unit.execute(
        sql.SQL("UPDATE runs SET {} WHERE id = %s").format(sql.SQL(", ").join(sets)),
        (*values, run_id),
    )


async def get(unit: ReadUnit, run_id: int) -> dict[str, Any] | None:
    return await (await unit.execute("SELECT * FROM runs WHERE id = %s", (run_id,))).fetchone()


async def recent(unit: ReadUnit, limit: int = 20) -> list[dict[str, Any]]:
    return await (
        await unit.execute("SELECT * FROM runs ORDER BY id DESC LIMIT %s", (limit,))
    ).fetchall()
