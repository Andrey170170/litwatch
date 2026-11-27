"""Release links (docs/specs/architecture/a2-work-gate.md, "Release links"): finding where a
paper's code and weights are released, on the deep service, and checking a placeholder again.

A find is of the paper as it is when submitted: at its arXiv version, or at its update when no
version is known (`store.links`). An automatic one runs when the analysis says a paper releases
code or weights, and again for each new version, until one is answered, "none found" included
(`due`, called where an analysis is recorded and wherever a new version is stored, `stored`); a
failed one is tried again by a later trigger, and by `retry` after the nightly's analyses,
`FIND_TRIES` failures at most. The owner's "find links" runs a fresh one each time (`request`).
Each is worked by `worker.FindWorker`, whose answer replaces the paper's links and is recorded
with its item's result in one unit (`record`), so an item tried again after that commit ends
without asking again; a find of what the paper no longer is ends with nothing replaced. A
placeholder link is checked again a week later, four times at most, each check its own item, by
a plain request to its host's API (`worker.RecheckWorker`, `adapter`); no other link is."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from litwatch.core.contracts import deep as contract
from litwatch.core.links import FIND_TRIES, FoundBy, next_check, rechecked_again, wants_find
from litwatch.core.work import WorkItem
from litwatch.gates.database import DatabaseGate, WriteUnit
from litwatch.links import adapter
from litwatch.store import links as store
from litwatch.store import work
from litwatch.store.papers import Upserted

# How far back `retry` looks for failed finds.
RETRY_WITHIN = timedelta(days=7)
# What a find that does not record its answer ends with.
SKIPPED = {"skipped": "litwatch no longer processes the paper"}
SUPERSEDED = {"superseded": "the paper has a newer version or update than this find was for"}


async def due(unit: WriteUnit, paper_ids: Iterable[str]) -> None:
    """Submit the automatic find of each paper whose analysis of its current text says it
    releases code or weights, as the paper now is, in paper id order, unless one of the same is
    open or answered, or has failed `FIND_TRIES` times: in a unit holding the papers' locks
    (a change of the paper, or intake's page)."""
    for paper_id in sorted(set(paper_ids)):
        facts = await store.release_facts(unit, paper_id)
        of = await store.identity(unit, paper_id)
        if facts is None or of is None or not wants_find(*facts):
            continue
        status = await store.find_status(unit, paper_id, of)
        if not (status.answered or status.open or status.failed >= FIND_TRIES):
            await store.submit_find(unit, paper_id, of, by="finder")


async def stored(unit: WriteUnit, upserted: Upserted, *, find_links: bool) -> None:
    """What storing papers asks of release links, in the unit that stored them: a paper whose
    version, or update when no version is known, changed is found again (`due`), where links
    are found (`word.Rules.find_links`)."""
    if find_links:
        await due(unit, upserted.versioned)


async def retry(db: DatabaseGate) -> None:
    """Submit again the automatic finds that failed lately (`due` decides), each paper in a
    unit of its own holding its lock."""
    async with db.read("release links: failed lately") as unit:
        failed = await store.failed_lately(unit, datetime.now(UTC) - RETRY_WITHIN)
    for paper_id in failed:
        async with db.write("release links: retry", taxonomy="shared", papers=[paper_id]) as unit:
            await due(unit, [paper_id])


async def request(unit: WriteUnit, paper_id: str) -> WorkItem | None:
    """The owner's "find links": a fresh find of the paper as it now is, whatever its analysis
    says, unless one of theirs is still open, which is the one asked for. None for a paper
    litwatch does not hold or no longer processes."""
    of = await store.identity(unit, paper_id)
    if of is None or of.dropped:
        return None
    for found in await store.open_finds(unit, paper_id):
        if found["by"] == "owner":
            item = await work.get(unit, found["id"])
            assert item is not None  # items are never deleted
            return item
    return await store.submit_find(unit, paper_id, of, by="owner")


async def record(
    unit: WriteUnit, item: WorkItem, answer: contract.LinksAnswer, result: dict[str, Any]
) -> dict[str, Any]:
    """The find's answer, in its item's unit, under the paper's shared row: unless the paper is
    dropped (`SKIPPED`) or is no longer what the find was of (`SUPERSEDED`), its links replaced
    by the answer, each placeholder's first re-check submitted, and `result` kept as the item's,
    by which a try of the item after this commits ends at once. Returns what the item ends
    with."""
    paper_id, by = item.scope["paper_id"], item.scope["by"]
    of = await store.identity(unit, paper_id)
    if of is None or of.dropped:
        return SKIPPED
    if of.find != item.scope["find"]:
        return SUPERSEDED
    found_by: FoundBy = "owner" if by == "owner" else "finder"
    for link in await store.replace(unit, paper_id, of.version, answer.links, found_by):
        if rechecked_again(link.state, link.rechecks) and adapter.checkable(link.url):
            await store.submit_recheck(unit, link, 1, next_check(link.checked_at))
    await work.put_result(unit, item, result)
    return result
