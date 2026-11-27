"""Release links (docs/specs/architecture/a2-work-gate.md, "Release links"): finding where a
paper's code and weights are released, on the deep service, and checking a placeholder again.

When a find runs is its item's key (`store.links`): an automatic one when the analysis says a
paper releases code or weights, once per paper and arXiv version for good, whatever it found
(`due`, called where an analysis is recorded and where a new version is stored); and the
owner's "find links", a fresh one each time (`request`). Each is worked by `worker.FindWorker`,
whose answer replaces the paper's links and is recorded with its item's result in one unit
(`record`), so an item tried again after that commit ends without asking again. A placeholder
link is checked again a week later, four times at most, by a plain request to its host's API
(`worker.RecheckWorker`, `hosts`); no other link is."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from litwatch.core.contracts import deep as contract
from litwatch.core.links import FoundBy, next_check, rechecked_again, wants_find
from litwatch.core.work import WorkItem
from litwatch.gates.database import WriteUnit
from litwatch.links import hosts
from litwatch.store import links as store
from litwatch.store import papers as papers_store
from litwatch.store import work


async def due(unit: WriteUnit, paper_ids: Iterable[str]) -> None:
    """Submit the automatic find of each paper whose analysis of its current text says it
    releases code or weights, at the paper's arXiv version, in paper id order: in the unit of
    the change that recorded the analysis, or of intake's page that stored the new version,
    which holds the papers' locks. A version found once is not found again."""
    for paper_id in sorted(set(paper_ids)):
        facts = await store.release_facts(unit, paper_id)
        if facts is not None and wants_find(facts[1], facts[2]):
            await store.submit_find(unit, paper_id, facts[0], by="finder")


async def request(unit: WriteUnit, paper_id: str) -> WorkItem | None:
    """The owner's "find links": a fresh find of the paper at its arXiv version, whatever its
    analysis says, unless one of theirs is still open, which is the one asked for. None for a
    paper litwatch does not hold or no longer processes."""
    if not await papers_store.held(unit, [paper_id]) or await papers_store.dropped(unit, paper_id):
        return None
    paper = await papers_store.get(unit, paper_id)
    assert paper is not None  # held, under the share lock `dropped` took
    if (found := await store.owners_find(unit, paper_id)) is not None:
        item = await work.get(unit, found["id"])
        assert item is not None  # items are never deleted
        return item
    return await store.submit_find(unit, paper_id, paper.version, by="owner")


async def record(
    unit: WriteUnit, item: WorkItem, answer: contract.LinksAnswer, result: dict[str, Any]
) -> None:
    """The find's answer, in its item's unit: the paper's links replaced by it, each
    placeholder's first re-check submitted, and `result` kept as the item's, by which a try of
    the item after this commits ends at once (`answered`)."""
    paper_id, version, by = item.scope["paper_id"], item.scope["version"], item.scope["by"]
    found_by: FoundBy = "owner" if by == "owner" else "finder"
    for link in await store.replace(unit, paper_id, version, answer.links, found_by):
        if rechecked_again(link.state, link.rechecks) and hosts.checkable(link.url):
            await store.submit_recheck(unit, link, next_check(link.checked_at))
    await work.put_result(unit, item, result)
