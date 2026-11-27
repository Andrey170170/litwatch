"""The Workers of release links (docs/plans/a2-work-gate.md, P6): a find of one paper's links on
the deep service (`FindWorker`, items of kind `store.links.FIND`) and a re-check of one
placeholder link at its host (`RecheckWorker`, kind `store.links.RECHECK`).

A find reads the paper's text from the archive first, waiting for it a while as a Deep pass
does, then goes on without it: the finder starts from the abstract and arXiv's comments. Its
submission is keyed by its item, so a try of the item after a lost answer follows the same
find. Its answer is recorded in the item's own unit (`links.record`), and a try after that
commit ends at once: no find is paid for twice. A service failure, or a find still running
when litwatch stops waiting, is tried again after `PAUSE`, as the same item; a
paper's failure (the finder never answered, the service turned the paper down) fails the item.

A re-check counts one of the link's four, whatever its host said, and submits the next for a
week later while the link is still a placeholder with re-checks left: the schedule is the
link's `rechecks` and its item's `not_before`, both rows."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from litwatch import links
from litwatch.archive import (
    URGENT,
    ArchiveClient,
    ArchiveError,
    ArchiveUnavailable,
    PaperText,
    read_text,
)
from litwatch.core.fulltext import TEXT_WAIT
from litwatch.core.links import RECHECK_EVERY, rechecked_again
from litwatch.core.work import WorkItem
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import Done, Later
from litwatch.integrations.deep import Deep, find_input
from litwatch.links import hosts
from litwatch.store import links as store
from litwatch.store import papers as papers_store
from litwatch.store import work

log = logging.getLogger(__name__)

# How many tries of a find wait for the paper's text (`TEXT_WAIT` apart) before it goes on
# without: about ten minutes.
TEXT_TRIES = 20
# How long a find the service failed waits before it is tried again: the Deep queue's pause.
PAUSE = timedelta(minutes=15)
SKIPPED = {"skipped": "litwatch no longer processes the paper"}


class FindError(RuntimeError):
    """A find the paper's failure ended: tried again it would end so again."""


class FindWorker:
    """Works finds on the deep service `deep`, reading papers' texts from `archive`."""

    def __init__(self, db: DatabaseGate, archive: ArchiveClient, deep: Deep) -> None:
        self.db, self.archive, self.deep = db, archive, deep

    async def __call__(self, item: WorkItem) -> Done | Later:
        paper_id = item.scope["paper_id"]
        async with self.db.write("find links: look") as unit:
            if answered := await work.results(unit, item.id):
                return Done(result=answered[-1][1])  # recorded by a try whose end was lost
            paper = await papers_store.get(unit, paper_id)
            gone = paper is None or await papers_store.dropped(unit, paper_id)
        if paper is None or gone:
            return Done(result=SKIPPED)
        text: PaperText | None = None
        try:
            text = await read_text(self.archive, paper, URGENT)
        except ArchiveUnavailable as exc:
            log.info("find links %s: the archive: %s", paper_id, exc)
            if item.attempts < TEXT_TRIES:
                return Later(datetime.now(UTC) + TEXT_WAIT)
        except ArchiveError as exc:  # no text to be had: the finder starts from the abstract
            log.info("find links %s: no text: %s", paper_id, exc)
        else:
            if text is None and item.attempts < TEXT_TRIES:  # the archive is fetching it
                return Later(datetime.now(UTC) + TEXT_WAIT)
        try:
            find_id = await self.deep.client.find(
                find_input(paper, text), key=f"links:{item.id}:{paper_id}"
            )
            found = await self.deep.client.follow_find(find_id)
        except httpx.HTTPError as exc:  # sent again by its key, it is the same find
            log.warning("find links %s: the deep service: %s", paper_id, exc)
            return Later(datetime.now(UTC) + PAUSE)
        if found.answer is None:
            if found.failure == "paper":
                raise FindError(found.error or "the find gave no answer")
            log.warning("find links %s: the deep service failed: %s", paper_id, found.error)
            return Later(datetime.now(UTC) + PAUSE)
        result = {"links": len(found.answer.links), "model": found.model, "usage": found.usage}
        async with self.db.write("record release links") as unit:
            # Dropped during the find (the owner dismissed it, arXiv withdrew it): kept as is.
            if await papers_store.dropped(unit, paper_id):
                return Done(result=SKIPPED)
            await links.record(unit, item, found.answer, result)
        return Done(result=result)


class RecheckWorker:
    """Works re-checks of placeholder links, over `http` (a client per check without one)."""

    def __init__(self, db: DatabaseGate, http: httpx.AsyncClient | None = None) -> None:
        self.db, self.http = db, http

    async def __call__(self, item: WorkItem) -> None:
        link_id = item.scope["link_id"]
        async with self.db.read("re-check a link: look") as unit:
            link = await store.get(unit, link_id)
        if link is None or not rechecked_again(link.state, link.rechecks):
            return  # replaced by a find since, or no longer due one
        checked = await hosts.check(link.url, self.http)
        at = datetime.now(UTC)
        async with self.db.write("re-check a link") as unit:
            link = await store.locked(unit, link_id)
            if link is None or not rechecked_again(link.state, link.rechecks):
                return
            link = await store.rechecked(unit, link_id, checked, at)
            if rechecked_again(link.state, link.rechecks):
                await store.submit_recheck(unit, link, at + RECHECK_EVERY)
