"""The outbox as integrations read it: events after a position, and one cursor per integration.

An event's place is its `Position`, (xid, id): the writing transaction's id, then the event's
own. Only events whose transaction is older than every transaction still running are read
(`_SETTLED`), so an event that commits late can never land behind a position already passed.

Each integration with a cursor is delivered to by work items of its own (docs/plans/a2-work-gate.md,
P2), one turn each, keyed and admitted by its name (`wake`): an event wakes, as it commits, those
of the integrations a process's `workers` row says it delivers to (`wake_all`), as the owner's
retry or skip wakes theirs.

The cursor's row serializes when its delivery runs: every write that schedules a turn (an
event's wake, the owner's retry or skip, a turn arranging the next) locks the cursor's row
before the item's, and reads the backoff under that lock. A backoff is recorded and cleared on
that row too, so no turn is scheduled by a backoff read before it changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple

from psycopg.types.json import Jsonb

from litwatch.core import Event
from litwatch.gates.database import ReadUnit, WriteUnit
from litwatch.store import work
from litwatch.store.text import stored

# The kind of the deliveries' work items.
DELIVERY = work.DELIVERY

_EVENT = "xid::text::bigint AS xid, id, type, paper_id, payload, created_at"
# Every transaction below the snapshot's xmin has finished: its events are all there is.
_SETTLED = "xid < pg_snapshot_xmin(pg_current_snapshot())"
_TOKEN = re.compile(r"^(\d{1,19})\.(\d{1,19})$")


class Position(NamedTuple):
    xid: int
    id: int

    @property
    def token(self) -> str:
        """The position as the changes feed hands it out: opaque to clients."""
        return f"{self.xid}.{self.id}"

    @classmethod
    def parse(cls, token: str) -> Position:
        if not (match := _TOKEN.match(token)):
            raise ValueError(f"not a position: {token!r}")
        return cls(int(match[1]), int(match[2]))

    @classmethod
    def of(cls, event: Event) -> Position:
        return cls(event.xid or 0, event.id or 0)


START = Position(0, 0)


@dataclass(frozen=True)
class Cursor:
    name: str
    position: Position
    failing: int | None = None
    failures: int = 0
    retry_at: datetime | None = None
    last_error: str | None = None
    failed_at: datetime | None = None
    skipped: int = 0
    lag: int = 0  # events after the position, wanted or not
    # While the integration's setup fails (no event is delivered meanwhile); 0 when it does not.
    setup_failures: int = 0
    setup_error: str | None = None
    setup_failed_at: datetime | None = None
    setup_retry_at: datetime | None = None


_FIELDS = (
    "name, position_xid::text::bigint AS position_xid, position, failing, failures, retry_at, "
    "last_error, failed_at, skipped, setup_failures, setup_error, setup_failed_at, setup_retry_at"
)


def _cursor(row: dict) -> Cursor:
    row = dict(row)
    row["position"] = Position(row.pop("position_xid"), row["position"])
    return Cursor(**row)


async def emit(unit: WriteUnit, type: str, paper_id: str | None = None, **payload: Any) -> int:
    """Record an event of `type` (`core.events`), in the unit that writes what it describes, and
    wake, as the unit commits, the delivery of every integration a process delivers to
    (`wake_all`). Returns the event's id."""
    row = await (
        await unit.execute(
            "INSERT INTO outbox (type, paper_id, payload) VALUES (%s, %s, %s) RETURNING id",
            (type, paper_id, Jsonb(payload)),
        )
    ).fetchone()
    unit.at_commit(DELIVERY, lambda: wake_all(unit))
    return row["id"]


async def wake_all(unit: WriteUnit) -> None:
    """Wake the delivery of every integration with a cursor that a process delivers to (`wake`):
    one whose `workers` row serves deliveries, for its keys or for all (`store.work.register`),
    whatever its heartbeat (D4: one `serve`'s row, or one killed, which its replacement
    supersedes as it registers). A cursor no one delivers to keeps its position and backoff,
    and gets its turn from the startup sweep of a `serve` that loads it. In the order of their
    names, so two emitters lock the cursors in one order: the last write of a unit that
    emitted, since every such unit locks those rows."""
    rows = await (
        await unit.execute(
            "SELECT c.name FROM integration_cursors c WHERE EXISTS (SELECT 1 FROM workers w "
            "WHERE %(kind)s = ANY(w.kinds) AND (NOT w.keys ? %(kind)s "
            "OR w.keys -> %(kind)s ? c.name)) ORDER BY c.name",
            {"kind": DELIVERY},
        )
    ).fetchall()
    for r in rows:
        await wake(unit, r["name"])


async def wake(unit: WriteUnit, name: str, *, at: datetime | None = None) -> None:
    """Submit the integration's delivery, to run no earlier than `at`; when not given, once the
    events may be delivered: now, or when the backoff of a failure or a failed setup the cursor
    records ends. The cursor's row is locked first, and the backoff read under that lock. A
    pending turn is brought forward to it, a turn waiting for the `idle` hook included; one due
    sooner keeps its time."""
    row = await (
        await unit.execute(
            "SELECT GREATEST(CASE WHEN failing IS NOT NULL THEN retry_at END, "
            "CASE WHEN setup_failures > 0 THEN setup_retry_at END) AS backoff "
            "FROM integration_cursors WHERE name = %s FOR NO KEY UPDATE",
            (name,),
        )
    ).fetchone()
    if at is None and row is not None:
        at = row["backoff"]
    await work.submit(unit, DELIVERY, name, admission=f"deliver:{name}", not_before=at)


async def unwoken(unit: ReadUnit, names: list[str]) -> list[str]:
    """The integrations of `names` with events after their cursor and no open turn: an event
    whose wake was missed (written while no `workers` row delivered to it yet, as `serve`
    started). In the order of their names."""
    rows = await (
        await unit.execute(
            "SELECT c.name FROM integration_cursors c WHERE c.name = ANY(%(names)s) "
            "AND NOT EXISTS (SELECT 1 FROM work_items w WHERE w.kind = %(kind)s "
            "AND w.key = c.name AND w.state <> 'ended') "
            "AND EXISTS (SELECT 1 FROM outbox o "
            "WHERE (o.xid, o.id) > (c.position_xid, c.position)) "
            "ORDER BY c.name",
            {"names": names, "kind": DELIVERY},
        )
    ).fetchall()
    return [r["name"] for r in rows]


async def waiting(unit: ReadUnit, name: str) -> tuple[bool, bool]:
    """Whether events wait after the integration's cursor: settled ones, which a delivery would
    read now, and any, those whose transaction an older one still running holds back
    included."""
    behind = (
        "SELECT 1 FROM outbox o JOIN integration_cursors c ON c.name = %(name)s "
        "WHERE (o.xid, o.id) > (c.position_xid, c.position)"
    )
    row = await (
        await unit.execute(
            f"SELECT EXISTS ({behind} AND {_SETTLED}) AS settled, EXISTS ({behind}) AS any",
            {"name": name},
        )
    ).fetchone()
    assert row is not None  # a SELECT of expressions has its row
    return row["settled"], row["any"]


async def after(unit: ReadUnit, position: Position, limit: int) -> list[Event]:
    """The settled events after `position`, in order."""
    rows = await (
        await unit.execute(
            f"SELECT {_EVENT} FROM outbox WHERE (xid, id) > (%s::text::xid8, %s) "
            f"AND {_SETTLED} ORDER BY xid, id LIMIT %s",
            (str(position.xid), position.id, limit),
        )
    ).fetchall()
    return [Event(**r) for r in rows]


async def of_paper(unit: ReadUnit, paper_id: str, type: str) -> list[Event]:
    """The paper's events of one type, settled or not, in the order they were written: a
    paper's changes take its lock, so their ids follow it."""
    rows = await (
        await unit.execute(
            f"SELECT {_EVENT} FROM outbox WHERE paper_id = %s AND type = %s ORDER BY id",
            (paper_id, type),
        )
    ).fetchall()
    return [Event(**r) for r in rows]


async def locked(unit: WriteUnit, name: str) -> Cursor | None:
    """The integration's cursor, its row locked for the unit before any item's (a turn
    arranging the next), or None while it has none: made by its first turn (`cursor`), not
    here, so this waits for no emitter."""
    found = await (
        await unit.execute(
            f"SELECT {_FIELDS} FROM integration_cursors WHERE name = %s FOR NO KEY UPDATE",
            (name,),
        )
    ).fetchone()
    return _cursor(found) if found is not None else None


async def cursor(unit: WriteUnit, name: str) -> Cursor:
    """The integration's cursor, made at the last settled event the first time it is asked
    for: an integration enabled for the first time is not woken by what came before it.

    It is made under the outbox's SHARE lock, which waits for every unit that wrote an event to
    end, and makes each that writes one meanwhile wait for this unit: so every emitter either
    ended before the cursor was made, its events placed as settled or not then, or looks up the
    cursors to wake (`wake_all`) after it was made. Without it, an emitter that looked before
    and committed after the first turn would leave events no turn is arranged for. Taken once
    for an integration's lifetime; each emitting write waits for it meanwhile."""
    found = await (
        await unit.execute(f"SELECT {_FIELDS} FROM integration_cursors WHERE name = %s", (name,))
    ).fetchone()
    if found is not None:
        return _cursor(found)
    await unit.execute("LOCK TABLE outbox IN SHARE MODE")
    await unit.execute(
        f"""
        INSERT INTO integration_cursors (name, position_xid, position)
        SELECT %s, coalesce(e.xid, '0'::xid8), coalesce(e.id, 0)
        FROM (SELECT 1) one LEFT JOIN LATERAL (
            SELECT xid, id FROM outbox WHERE {_SETTLED} ORDER BY xid DESC, id DESC LIMIT 1
        ) e ON true
        ON CONFLICT (name) DO NOTHING
        """,
        (name,),
    )
    row = await (
        await unit.execute(f"SELECT {_FIELDS} FROM integration_cursors WHERE name = %s", (name,))
    ).fetchone()
    return _cursor(row)


async def cursors(unit: ReadUnit, names: list[str]) -> dict[str, Cursor]:
    """The named cursors that exist, each with its lag."""
    rows = await (
        await unit.execute(
            f"SELECT {_FIELDS}, (SELECT count(*) FROM outbox o "
            "WHERE (o.xid, o.id) > (c.position_xid, c.position)) AS lag "
            "FROM integration_cursors c WHERE name = ANY(%s)",
            (names,),
        )
    ).fetchall()
    return {r["name"]: _cursor(r) for r in rows}


async def setup_failing(unit: ReadUnit, name: str) -> bool:
    """Whether the integration's last setup failed, in whichever process ran it."""
    row = await (
        await unit.execute(
            "SELECT 1 FROM integration_cursors WHERE name = %s AND setup_failures > 0", (name,)
        )
    ).fetchone()
    return row is not None


async def failing(unit: ReadUnit, names: list[str]) -> set[str]:
    """Those of the named cursors stuck on an event or on their setup: what a card shows as
    failing, without the lag `cursors` counts."""
    rows = await (
        await unit.execute(
            "SELECT name FROM integration_cursors WHERE name = ANY(%s) "
            "AND (failing IS NOT NULL OR setup_failures > 0)",
            (names,),
        )
    ).fetchall()
    return {r["name"] for r in rows}


async def advance(unit: WriteUnit, name: str, position: Position) -> None:
    """Everything up to `position` is done; a failure before it is over."""
    await unit.execute(
        "UPDATE integration_cursors SET position_xid = %s::text::xid8, position = %s, "
        "failing = NULL, failures = 0, retry_at = NULL, updated_at = now() "
        "WHERE name = %s AND (position_xid, position) < (%s::text::xid8, %s)",
        (str(position.xid), position.id, name, str(position.xid), position.id),
    )


async def failed(
    unit: WriteUnit,
    name: str,
    event: Event,
    failures: int,
    error: str,
    failed_at: datetime,
    retry_at: datetime,
) -> bool:
    """Record that `event` failed, unless the cursor has meanwhile gone past it (the owner
    skipped it while it was being tried). Returns whether it was recorded."""
    at = Position.of(event)
    cur = await unit.execute(
        "UPDATE integration_cursors SET failing = %s, failures = %s, last_error = %s, "
        "failed_at = %s, retry_at = %s, updated_at = now() "
        "WHERE name = %s AND (position_xid, position) < (%s::text::xid8, %s)",
        (event.id, failures, stored(error), failed_at, retry_at, name, str(at.xid), at.id),
    )
    return cur.rowcount > 0


async def setup_failed(
    unit: WriteUnit,
    name: str,
    failures: int,
    error: str,
    failed_at: datetime,
    retry_at: datetime,
) -> None:
    """Record that the integration's setup failed for the `failures`th time in a row."""
    await unit.execute(
        "UPDATE integration_cursors SET setup_failures = %s, setup_error = %s, "
        "setup_failed_at = %s, setup_retry_at = %s, updated_at = now() WHERE name = %s",
        (failures, stored(error), failed_at, retry_at, name),
    )


async def setup_done(unit: WriteUnit, name: str) -> None:
    """The integration's setup succeeded: a failure before it is over."""
    await unit.execute(
        "UPDATE integration_cursors SET setup_failures = 0, setup_error = NULL, "
        "setup_failed_at = NULL, setup_retry_at = NULL, updated_at = now() "
        "WHERE name = %s AND setup_failures > 0",
        (name,),
    )


async def retry(unit: WriteUnit, name: str) -> bool:
    """Try the failing event, or the failing setup, again on the next pass, without waiting
    out the backoff."""
    cur = await unit.execute(
        "UPDATE integration_cursors SET retry_at = NULL, setup_retry_at = NULL, "
        "updated_at = now() WHERE name = %s AND (failing IS NOT NULL OR setup_failures > 0)",
        (name,),
    )
    if cur.rowcount:
        await wake(unit, name)
    return cur.rowcount > 0


async def skip(unit: WriteUnit, name: str, expected: int | None = None) -> int | None:
    """Give up on the failing event and go on after it, only if it is `expected` when one
    is given (the event the owner confirmed). Returns its id, or None when nothing was
    skipped."""
    row = await (
        await unit.execute(
            "UPDATE integration_cursors c SET position_xid = o.xid, position = o.id, "
            "failing = NULL, failures = 0, retry_at = NULL, skipped = skipped + 1, "
            "updated_at = now() FROM outbox o "
            "WHERE c.name = %s AND o.id = c.failing "
            "AND (%s::bigint IS NULL OR c.failing = %s) RETURNING o.id",
            (name, expected, expected),
        )
    ).fetchone()
    if row is None:
        return None
    await wake(unit, name)
    return row["id"]


async def event(unit: ReadUnit, event_id: int) -> Event | None:
    row = await (
        await unit.execute(f"SELECT {_EVENT} FROM outbox WHERE id = %s", (event_id,))
    ).fetchone()
    return Event(**row) if row else None
