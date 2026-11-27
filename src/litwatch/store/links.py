"""Release links and the work that keeps them (data.md: `release_links`; docs/plans/a2-work-gate.md,
P6): finds, work items of kind `FIND`, and re-checks of a placeholder, of kind `RECHECK`.

What a find is of is its scope's `find` (`core.links.find_key`: the paper at an arXiv version, or
at an update when no version is known). An automatic find is keyed by it; the owner's by it and a
fresh uuid, so each runs. An automatic find runs while none of the same is open or answered
(`find_status`): only an answered find, "none found" included, holds it for good; one that failed
leaves its failure on record and lets a later trigger try again, `FIND_TRIES` failures at most.
A find is answered when its item ends done with its answer's count (`result.links`). A re-check
is keyed by its link and its number (`core.links.recheck_key`); its schedule is its item's
`not_before` and the link's `rechecks`.

Lock order. A find's answer is written in its item's unit: the item's row (the fence), the
paper's row shared (`identity`), then the paper's links, deleted and inserted (finds are admitted
one at a time), then each placeholder's first re-check item, in link id order. A re-check's
unit: its item's row (the fence), its link's row (`locked`), then the next re-check's item. A
submission of a find takes the paper's lock before the item's row, as every settle does (a
change of the paper, or intake's page)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from litwatch.core.contracts import deep as contract
from litwatch.core.links import FoundBy, ReleaseLink, State, find_key, recheck_key
from litwatch.core.work import WorkItem
from litwatch.gates.database import ReadUnit, WriteUnit
from litwatch.store import work

FIND = "links"
RECHECK = "link check"
# The owner's finds go before the automatic ones.
OWNERS_PRIORITY, AUTOMATIC_PRIORITY = 0, 1

_COLUMNS = "id, paper_id, version, url, kind, state, license, checked_at, found_by, rechecks"


@dataclass(frozen=True)
class Identity:
    """What a paper is now, as a find is of it: whether litwatch no longer processes it, and
    what a find of it now is of (`core.links.find_key`), with its version and update."""

    dropped: bool
    version: int | None
    updated: str | None
    find: str


async def identity(unit: WriteUnit, paper_id: str) -> Identity | None:
    """The paper's identity, its row shared to the end of the unit, so a version or a dismissal
    being stored is waited for; None for a paper not held."""
    row = await (
        await unit.execute(
            "SELECT dropped, version, updated FROM papers WHERE id = %s FOR SHARE", (paper_id,)
        )
    ).fetchone()
    if row is None:
        return None
    updated = row["updated"].astimezone(UTC).isoformat() if row["updated"] else None
    key = find_key(paper_id, row["version"], updated)
    return Identity(row["dropped"], row["version"], updated, key)


async def release_facts(unit: ReadUnit, paper_id: str) -> tuple[bool, bool] | None:
    """Whether the paper's current analysis says it releases code, and weights; None for a
    paper dropped or not analysed on its current text."""
    row = await (
        await unit.execute(
            "SELECT a.releases_code, a.releases_weights FROM papers p "
            "JOIN analyses a ON a.paper_id = p.id AND a.paper_revision = p.revision "
            "WHERE p.id = %s AND NOT p.dropped ORDER BY a.id DESC LIMIT 1",
            (paper_id,),
        )
    ).fetchone()
    return (row["releases_code"], row["releases_weights"]) if row else None


async def submit_find(unit: WriteUnit, paper_id: str, of: Identity, *, by: FoundBy) -> WorkItem:
    """Submit a find of the paper as `of` is: the finder's, keyed by what it is of, so a
    submission while one is open is that one; the owner's, a fresh one."""
    scope = {
        "paper_id": paper_id,
        "version": of.version,
        "updated": of.updated,
        "find": of.find,
        "by": by,
    }
    if by == "owner":
        key, priority = f"{of.find}#{uuid4()}", OWNERS_PRIORITY
    else:
        key, priority = of.find, AUTOMATIC_PRIORITY
    return await work.submit(unit, FIND, key, scope=scope, priority=priority)


@dataclass(frozen=True)
class FindStatus:
    answered: bool  # a find of the same ended with an answer
    open: bool  # one is pending or running
    failed: int  # the finder's that failed


async def find_status(unit: ReadUnit, paper_id: str, of: Identity) -> FindStatus:
    """The paper's finds of what `of` is of, or of its same update when that find knew no
    version (a version learnt since for the update found is no new find)."""
    row = await (
        await unit.execute(
            "SELECT COALESCE(bool_or(state = 'ended' AND outcome = 'done' AND result ? 'links'), "
            "false) AS answered, COALESCE(bool_or(state <> 'ended'), false) AS open, "
            "count(*) FILTER (WHERE outcome = 'failed' AND scope ->> 'by' = 'finder') AS failed "
            "FROM work_items WHERE kind = %s AND scope ->> 'paper_id' = %s "
            "AND (scope ->> 'find' = %s "
            "OR (scope ->> 'version' IS NULL AND scope ->> 'updated' = %s))",
            (FIND, paper_id, of.find, of.updated),
        )
    ).fetchone()
    assert row is not None  # an aggregate
    return FindStatus(row["answered"], row["open"], row["failed"])


async def failed_lately(unit: ReadUnit, since: datetime) -> list[str]:
    """The papers an automatic find failed for since `since`, in id order."""
    rows = await (
        await unit.execute(
            "SELECT DISTINCT scope ->> 'paper_id' AS paper_id FROM work_items "
            "WHERE kind = %s AND outcome = 'failed' AND scope ->> 'by' = 'finder' "
            "AND ended_at > %s ORDER BY 1",
            (FIND, since),
        )
    ).fetchall()
    return [r["paper_id"] for r in rows]


_FIND_COLUMNS = (
    "id, state, outcome, error, result, scope ->> 'by' AS by, "
    "COALESCE(ended_at, started_at, not_before) AS at"
)


async def last_finished(unit: ReadUnit, paper_id: str) -> dict[str, Any] | None:
    """The paper's find that ended last (its item: state, outcome, error, result, by, and when
    it ended), if one has."""
    return await (
        await unit.execute(
            f"SELECT {_FIND_COLUMNS} FROM work_items WHERE kind = %s "
            "AND scope ->> 'paper_id' = %s AND state = 'ended' ORDER BY ended_at DESC, id DESC "
            "LIMIT 1",
            (FIND, paper_id),
        )
    ).fetchone()


async def open_finds(unit: ReadUnit, paper_id: str) -> list[dict[str, Any]]:
    """The paper's finds pending or running, the owner's first, then the oldest."""
    return await (
        await unit.execute(
            f"SELECT {_FIND_COLUMNS} FROM work_items WHERE kind = %s "
            "AND scope ->> 'paper_id' = %s AND state <> 'ended' "
            "ORDER BY (scope ->> 'by') <> 'owner', id",
            (FIND, paper_id),
        )
    ).fetchall()


def _link(row: dict[str, Any]) -> ReleaseLink:
    return ReleaseLink(**row)


async def of_paper(unit: ReadUnit, paper_id: str) -> list[ReleaseLink]:
    """The paper's links, code first, each kind in the order its find gave them."""
    rows = await (
        await unit.execute(
            f"SELECT {_COLUMNS} FROM release_links WHERE paper_id = %s ORDER BY kind, id",
            (paper_id,),
        )
    ).fetchall()
    return [_link(r) for r in rows]


async def replace(
    unit: WriteUnit,
    paper_id: str,
    version: int | None,
    links: list[contract.ReleaseLink],
    found_by: FoundBy,
) -> list[ReleaseLink]:
    """The paper's links, as a find of `version` answered them, in place of those it had."""
    await unit.execute("DELETE FROM release_links WHERE paper_id = %s", (paper_id,))
    stored: list[ReleaseLink] = []
    for link in links:
        row = await (
            await unit.execute(
                "INSERT INTO release_links (paper_id, version, url, kind, state, license, "
                "checked_at, found_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_COLUMNS}",
                (
                    paper_id,
                    version,
                    link.url,
                    link.kind,
                    link.state,
                    link.license,
                    datetime.fromisoformat(link.checked_at),
                    found_by,
                ),
            )
        ).fetchone()
        assert row is not None  # the insert returns it
        stored.append(_link(row))
    return stored


async def submit_recheck(unit: WriteUnit, link: ReleaseLink, check: int, at: datetime) -> None:
    """Submit the link's `check`th re-check, for `at`: the first from its find's unit, each
    next from the unit of the one before it, which records that one."""
    await work.submit(
        unit,
        RECHECK,
        recheck_key(link.id, check),
        scope={"link_id": link.id, "paper_id": link.paper_id, "check": check},
        not_before=at,
    )


async def get(unit: ReadUnit, link_id: int) -> ReleaseLink | None:
    """The link; None once a find replaced it."""
    row = await (
        await unit.execute(f"SELECT {_COLUMNS} FROM release_links WHERE id = %s", (link_id,))
    ).fetchone()
    return _link(row) if row else None


async def locked(unit: WriteUnit, link_id: int) -> ReleaseLink | None:
    """`get`, its row locked to the end of the unit."""
    row = await (
        await unit.execute(
            f"SELECT {_COLUMNS} FROM release_links WHERE id = %s FOR UPDATE", (link_id,)
        )
    ).fetchone()
    return _link(row) if row else None


async def rechecked(
    unit: WriteUnit, link_id: int, checked: tuple[State, str | None] | None, at: datetime
) -> ReleaseLink:
    """Count a re-check of the link, locked in this unit (`locked`): with what it found, its
    state and license as of `at`; without (the host could not say), only the count."""
    if checked is None:
        query = (
            f"UPDATE release_links SET rechecks = rechecks + 1 WHERE id = %s RETURNING {_COLUMNS}"
        )
        params: tuple[Any, ...] = (link_id,)
    else:
        query = (
            "UPDATE release_links SET rechecks = rechecks + 1, state = %s, "
            "license = COALESCE(%s, license), checked_at = %s, found_by = 'recheck' "
            f"WHERE id = %s RETURNING {_COLUMNS}"
        )
        params = (*checked, at, link_id)
    row = await (await unit.execute(query, params)).fetchone()
    assert row is not None  # locked in this unit
    return _link(row)
