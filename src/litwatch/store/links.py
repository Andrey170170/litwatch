"""Release links and the work that keeps them (data.md: `release_links`; docs/plans/a2-work-gate.md,
P6): finds, work items of kind `FIND`, and re-checks of a placeholder, of kind `RECHECK`.

A find's key decides whether it runs: an automatic one is keyed by the paper and its arXiv version
and held for good (`core.links.find_key`), so it runs once per version whatever it found; the
owner's is keyed afresh each time. Each finds item carries its paper in its scope, by which the
paper's finds are read (`latest_find`, `owners_find`). A re-check is keyed by its link, and its
schedule is its item's `not_before` and the link's `rechecks`.

Lock order. A find's answer is written in its item's unit: the item's row (the fence), then the
paper's links, deleted and inserted (finds are admitted one at a time), then each placeholder's
re-check item, in link id order. A re-check's unit: its item's row (the fence), its link's row
(`locked`), then its own item's next turn. A submission of a find takes the paper's lock
before the item's row, as every settle does (a change of the paper, or intake's page)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from litwatch.core.contracts import deep as contract
from litwatch.core.links import FoundBy, ReleaseLink, State, find_key
from litwatch.core.work import WorkItem
from litwatch.gates.database import ReadUnit, WriteUnit
from litwatch.store import work

FIND = "links"
RECHECK = "link check"
# The owner's finds go before the automatic ones.
OWNERS_PRIORITY, AUTOMATIC_PRIORITY = 0, 1

_COLUMNS = "id, paper_id, version, url, kind, state, license, checked_at, found_by, rechecks"


async def release_facts(unit: ReadUnit, paper_id: str) -> tuple[int | None, bool, bool] | None:
    """The paper's arXiv version and whether its current analysis says it releases code, and
    weights; None for a paper dropped or not analysed on its current text."""
    row = await (
        await unit.execute(
            "SELECT p.version, a.releases_code, a.releases_weights FROM papers p "
            "JOIN analyses a ON a.paper_id = p.id AND a.paper_revision = p.revision "
            "WHERE p.id = %s AND NOT p.dropped ORDER BY a.id DESC LIMIT 1",
            (paper_id,),
        )
    ).fetchone()
    return (row["version"], row["releases_code"], row["releases_weights"]) if row else None


async def submit_find(
    unit: WriteUnit, paper_id: str, version: int | None, *, by: FoundBy
) -> WorkItem:
    """Submit a find of the paper's links at `version`: the finder's, once for good per version
    (a key held after it ended returns its item); the owner's, a fresh one."""
    scope = {"paper_id": paper_id, "version": version, "by": by}
    if by == "owner":
        return await work.submit(
            unit,
            FIND,
            f"{find_key(paper_id, version)}#{uuid4()}",
            scope=scope,
            priority=OWNERS_PRIORITY,
        )
    return await work.submit(
        unit,
        FIND,
        find_key(paper_id, version),
        scope=scope,
        lasting=True,
        priority=AUTOMATIC_PRIORITY,
    )


_FIND_COLUMNS = (
    "id, state, outcome, error, result, scope ->> 'by' AS by, "
    "COALESCE(ended_at, started_at, not_before) AS at"
)


async def latest_find(unit: ReadUnit, paper_id: str) -> dict[str, Any] | None:
    """The paper's newest find (its item: state, outcome, error, result, by, and when it ended,
    started or is due), if it has had one."""
    return await (
        await unit.execute(
            f"SELECT {_FIND_COLUMNS} FROM work_items WHERE kind = %s "
            "AND scope ->> 'paper_id' = %s ORDER BY id DESC LIMIT 1",
            (FIND, paper_id),
        )
    ).fetchone()


async def owners_find(unit: ReadUnit, paper_id: str) -> dict[str, Any] | None:
    """The owner's find of the paper still open (pending or running), if any."""
    return await (
        await unit.execute(
            f"SELECT {_FIND_COLUMNS} FROM work_items WHERE kind = %s "
            "AND scope ->> 'paper_id' = %s AND scope ->> 'by' = 'owner' AND state <> 'ended' "
            "ORDER BY id DESC LIMIT 1",
            (FIND, paper_id),
        )
    ).fetchone()


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


async def submit_recheck(unit: WriteUnit, link: ReleaseLink, at: datetime) -> None:
    """Submit the link's next re-check, for `at`: its first from its find's unit, each next
    from the re-check before it, which submits its own key while it runs."""
    await work.submit(
        unit,
        RECHECK,
        str(link.id),
        scope={"link_id": link.id, "paper_id": link.paper_id},
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
