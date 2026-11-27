"""Release links (docs/specs/architecture/a2-work-gate.md, "Release links"): finding where a
paper's code and weights are released, on the deep service, and checking a placeholder again.

A find is of the paper as it is when submitted (`core.links.Identity`: its arXiv version, or its
update when no version is known), and every decision here asks whether a find is of the paper as
it is now (`Identity.covers`). An automatic find runs when the analysis says a paper releases
code or weights (`due`, called where an analysis is recorded and wherever another identity is
stored, `stored`), unless the paper has an answer of its identity, "none found" included, the
owner's or the finder's, or an automatic find of it is open, or `FIND_TRIES` of them failed; a
failed one is tried again by a later trigger, and by `retry` in the nightly. The owner's "find
links" runs a fresh one each time (`request`), unless one of theirs of the same identity is open.

Each is worked by `worker.FindWorker`. Before it pays (`check`), and again with its answer
(`record`), in a unit holding the paper's locks: a find of a dropped paper ends `SKIPPED`; one of
what the paper no longer is ends `SUPERSEDED` and hands over (`hand_over`): the owner's is asked
again of the paper as it is, an automatic one is due again; an automatic find whose identity
another find answered meanwhile ends `ANSWERED` without paying. An answer replaces the paper's
links and is recorded with its item's result in one unit, so an item tried again after that
commit ends without asking again. A placeholder link is checked again a week later, four times
at most, each check its own item, by a plain request to its host's API (`worker.RecheckWorker`,
`adapter`); no other link is."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from litwatch.core.contracts import deep as contract
from litwatch.core.links import (
    FIND_TRIES,
    Find,
    FoundBy,
    Identity,
    next_check,
    rechecked_again,
    wants_find,
)
from litwatch.core.work import WorkItem
from litwatch.gates.database import DatabaseGate, WriteUnit
from litwatch.links import adapter
from litwatch.store import links as store
from litwatch.store import work
from litwatch.store.papers import Upserted

# How far back `retry` looks for failed finds.
RETRY_WITHIN = timedelta(days=7)
# What a find that does not record an answer ends with.
SKIPPED = {"skipped": "litwatch no longer processes the paper"}
SUPERSEDED = {"superseded": "the paper has a newer version or update than this find was for"}
ANSWERED = {"skipped": "another find answered for the paper as it is"}


def covering(finds: list[Find], now: Identity) -> list[Find]:
    """The finds of the paper as it is `now`."""
    return [f for f in finds if f.of.covers(now)]


def automatic_due(finds: list[Find], now: Identity) -> bool:
    """Whether the paper, as it is `now`, is due an automatic find: none of its finds of it
    answered, no automatic one is open, and fewer than `FIND_TRIES` automatic ones failed. An
    open find of the owner's does not count: should it fail, the automatic one still runs."""
    of_now = covering(finds, now)
    automatic = [f for f in of_now if f.by == "finder"]
    return not (
        any(f.answered for f in of_now)
        or any(f.open for f in automatic)
        or sum(f.outcome == "failed" for f in automatic) >= FIND_TRIES
    )


async def due(unit: WriteUnit, paper_ids: Iterable[str]) -> None:
    """Submit the automatic find of each paper whose analysis of its current text says it
    releases code or weights, as the paper now is, in paper id order, where it is due
    (`automatic_due`): in a unit holding the papers' locks (a change of the paper, or intake's
    page)."""
    for paper_id in sorted(set(paper_ids)):
        facts = await store.release_facts(unit, paper_id)
        now = await store.identity(unit, paper_id)
        if facts is None or now is None or now.dropped or not wants_find(*facts):
            continue
        if automatic_due(await store.finds(unit, paper_id), now.of):
            await store.submit_find(unit, paper_id, now.of, by="finder")


async def stored(unit: WriteUnit, upserted: Upserted, *, find_links: bool) -> None:
    """What storing papers asks of release links, in the unit that stored them: a paper that is
    another for release links than it was (`Upserted.versioned`) is found again (`due`), where
    links are found (`word.Rules.find_links`)."""
    if find_links:
        await due(unit, upserted.versioned)


async def retry(db: DatabaseGate) -> None:
    """Submit again the automatic finds that failed lately (`due` decides), each paper in a
    unit of its own holding its locks: the nightly's, once a night."""
    async with db.read("release links: failed lately") as unit:
        failed = await store.failed_lately(unit, datetime.now(UTC) - RETRY_WITHIN)
    for paper_id in failed:
        async with db.write("release links: retry", taxonomy="shared", papers=[paper_id]) as unit:
            await due(unit, [paper_id])


async def request(unit: WriteUnit, paper_id: str) -> WorkItem | None:
    """The owner's "find links": a fresh find of the paper as it now is, whatever its analysis
    says, unless one of theirs of the same is open, which is the one asked for. None for a
    paper litwatch does not hold or no longer processes. In a unit holding the paper's locks."""
    now = await store.identity(unit, paper_id)
    if now is None or now.dropped:
        return None
    for found in covering(await store.finds(unit, paper_id), now.of):
        if found.by == "owner" and found.open:
            item = await work.get(unit, found.id)
            assert item is not None  # items are never deleted
            return item
    return await store.submit_find(unit, paper_id, now.of, by="owner")


async def hand_over(unit: WriteUnit, item: WorkItem) -> None:
    """A find of what the paper no longer is hands over to the paper as it is: the owner's is
    asked again (`request`), an automatic one is due again (`due`)."""
    paper_id = item.scope["paper_id"]
    if item.scope["by"] == "owner":
        await request(unit, paper_id)
    else:
        await due(unit, [paper_id])


async def _ends(unit: WriteUnit, item: WorkItem) -> tuple[store.Now | None, dict[str, Any] | None]:
    """The paper as it is, and what the find ends with instead of an answer, if anything: a
    dropped paper's `SKIPPED`; `SUPERSEDED` (handed over) when it is no longer what the find
    was of."""
    now = await store.identity(unit, item.scope["paper_id"])
    if now is None or now.dropped:
        return now, SKIPPED
    if not Identity.of(item.scope).covers(now.of):
        await hand_over(unit, item)
        return now, SUPERSEDED
    return now, None


async def check(unit: WriteUnit, item: WorkItem) -> dict[str, Any] | None:
    """Before the find pays, in its item's unit holding the paper's locks: what it ends with
    instead (`_ends`, or `ANSWERED` for an automatic find whose identity another answered),
    or None when it is to be asked."""
    now, ended = await _ends(unit, item)
    if ended is not None or item.scope["by"] == "owner":
        return ended
    assert now is not None
    finds = covering(await store.finds(unit, item.scope["paper_id"]), now.of)
    return ANSWERED if any(f.answered and f.id != item.id for f in finds) else None


async def record(
    unit: WriteUnit, item: WorkItem, answer: contract.LinksAnswer, result: dict[str, Any]
) -> dict[str, Any]:
    """The find's answer, in its item's unit holding the paper's locks: unless the find ends
    otherwise (`_ends`), the paper's links replaced by the answer, each placeholder's first
    re-check submitted, and `result` kept as the item's, by which a try of the item after this
    commits ends at once. Returns what the item ends with."""
    now, ended = await _ends(unit, item)
    if ended is not None:
        return ended
    assert now is not None
    found_by: FoundBy = "owner" if item.scope["by"] == "owner" else "finder"
    paper_id = item.scope["paper_id"]
    for link in await store.replace(unit, paper_id, now.of.version, answer.links, found_by):
        if rechecked_again(link.state, link.rechecks) and adapter.checkable(link.url):
            await store.submit_recheck(unit, link, 1, next_check(link.checked_at))
    await work.put_result(unit, item, result)
    return result
