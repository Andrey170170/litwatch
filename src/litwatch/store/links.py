"""Release links and the work that keeps them (data.md: `release_links`; docs/plans/a2-work-gate.md,
P6): finds, work items of kind `FIND`, and re-checks of a placeholder, of kind `RECHECK`.

What a find is of is its scope's `version` and `updated` (`core.links.Identity`), and whether it
is of the paper as it is now is `Identity.covers`, which `litwatch.links` decides with: this
module only stores and reads. An automatic find is keyed by its identity; the owner's by it and
a fresh uuid, so each runs. A find is answered when its item ends done with its answer's count
(`result.links`). A re-check is keyed by its link and its number (`core.links.recheck_key`); its
schedule is its item's `not_before` and the link's `rechecks`.

Lock order. A find's check before it pays, and its answer, are written in a unit of its item
(the fence) holding the paper's locks (the taxonomy shared, then the paper), as every change
takes them: then the paper's row shared (`identity`), then the paper's links, deleted and
inserted, then each placeholder's first re-check item in link id order, or the find handed over
to the paper as it now is. A re-check's unit: its item's row (the fence), its link's row
(`locked`), then the next re-check's item. A submission of a find takes the paper's locks before
the item's row, as every settle does (a change of the paper, or intake's page)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from litwatch.core.contracts import deep as contract
from litwatch.core.links import Find, FoundBy, Identity, ReleaseLink, State, recheck_key
from litwatch.core.work import WorkItem
from litwatch.gates.database import ReadUnit, WriteUnit
from litwatch.store import work

FIND = "links"
RECHECK = "link check"
# The owner's finds go before the automatic ones.
OWNERS_PRIORITY, AUTOMATIC_PRIORITY = 0, 1

_COLUMNS = "id, paper_id, version, url, kind, state, license, checked_at, found_by, rechecks"


@dataclass(frozen=True)
class Now:
    """A paper as a find is of it now: whether litwatch no longer processes it, and what a find
    of it is of (`core.links.Identity`)."""

    dropped: bool
    of: Identity


_NOW = "SELECT dropped, version, updated FROM papers WHERE id = %s"


def _now(row: dict[str, Any] | None) -> Now | None:
    return Now(row["dropped"], Identity.at(row["version"], row["updated"])) if row else None


async def identity(unit: WriteUnit, paper_id: str) -> Now | None:
    """The paper as a find is of it now, its row shared to the end of the unit, so a version or
    a dismissal being stored is waited for; None for a paper not held."""
    return _now(await (await unit.execute(f"{_NOW} FOR SHARE", (paper_id,))).fetchone())


async def current(unit: ReadUnit, paper_id: str) -> Now | None:
    """`identity`, read without a lock (the page's)."""
    return _now(await (await unit.execute(_NOW, (paper_id,))).fetchone())


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
    """Submit a find of the paper as `of` is: the finder's, keyed by it (`Identity.key`), so a
    submission while one is open is that one; the owner's, a fresh one."""
    key = of.key(paper_id)
    scope = {"paper_id": paper_id, "version": of.version, "updated": of.updated}
    if by == "owner":
        key, priority = f"{key}#{uuid4()}", OWNERS_PRIORITY
    else:
        key, priority = key, AUTOMATIC_PRIORITY
    return await work.submit(unit, FIND, key, scope=scope | {"by": by}, priority=priority)


def _answered(answer: Any) -> bool:
    return isinstance(answer, dict) and isinstance(answer.get("links"), int)


async def finds(unit: ReadUnit, paper_id: str) -> list[Find]:
    """The paper's finds, oldest first. A find answered once its answer is recorded: while its
    item is open, by the result `links.record` keeps with it in the unit that writes the links;
    once the Work gate ends the item, in a commit of its own, by the item's own `result`, the
    answer's durable record (an ended item's `work_results` may be pruned)."""
    rows = await (
        await unit.execute(
            "SELECT w.id, w.state, w.outcome, w.error, w.scope, "
            "CASE WHEN w.state = 'ended' THEN w.result ELSE a.body END AS answer, "
            "COALESCE(w.ended_at, w.started_at, w.not_before) AS at "
            "FROM work_items w LEFT JOIN LATERAL (SELECT body FROM work_results r "
            "WHERE w.state <> 'ended' AND r.work_id = w.id AND r.body ? 'links' "
            "ORDER BY r.id DESC LIMIT 1) a ON true "
            "WHERE w.kind = %s AND w.scope ->> 'paper_id' = %s ORDER BY w.id",
            (FIND, paper_id),
        )
    ).fetchall()
    return [
        Find(
            id=r["id"],
            by="owner" if r["scope"].get("by") == "owner" else "finder",
            of=Identity.of(r["scope"]),
            state=r["state"],
            outcome=r["outcome"],
            answered=_answered(r["answer"]),
            links=r["answer"]["links"] if _answered(r["answer"]) else None,
            error=r["error"],
            at=r["at"],
        )
        for r in rows
    ]


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
