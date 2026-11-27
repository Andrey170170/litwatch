"""The Workers of release links (docs/plans/a2-work-gate.md, P6): a find of one paper's links on
the deep service (`FindWorker`, items of kind `store.links.FIND`) and a re-check of one
placeholder link at its host (`RecheckWorker`, kind `store.links.RECHECK`).

A find is of the paper as it was when submitted (`store.links.Identity`): one of what the paper
no longer is ends `links.SUPERSEDED` before it is sent, and its answer replaces no links. A find
reads the paper's text from the archive first, waiting for it a while as a Deep pass does, then
goes on without it: the finder starts from the abstract and arXiv's comments. Its submission is
keyed by its item, so while its end is not known (no reply, litwatch stopped waiting, the
service lost it) the item is tried again after `PAUSE` and follows the same find. Its answer is
recorded in the item's own unit (`links.record`), its end too when it failed (the finder never
answered, the service turned it down or broke), with the usage the service reported, and a try
after that commit ends as it did: no find is paid for twice, nor a failure replayed. A failed
find fails its item; a later trigger submits a fresh one (`links.due`).

A re-check is the link's `check`th of four: its count, what it found and the next check, a
week later, commit in the unit it ends with, so a try after that commit finds the count past it
and asks the host nothing."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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
from litwatch.core.links import RECHECK_EVERY, ReleaseLink, rechecked_again
from litwatch.core.work import WorkItem
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import Done, Later
from litwatch.integrations.deep import Deep, find_input
from litwatch.links import adapter
from litwatch.store import links as store
from litwatch.store import papers as papers_store
from litwatch.store import work

log = logging.getLogger(__name__)

# How many tries of a find wait for the paper's text (`TEXT_WAIT` apart) before it goes on
# without: about ten minutes.
TEXT_TRIES = 20
# How long a find whose end is not known waits before it is followed again: the Deep queue's
# pause.
PAUSE = timedelta(minutes=15)


class FindError(RuntimeError):
    """A find that ended without an answer: tried again by its key it would end so again."""


def _cost(usage: dict[str, Any] | None) -> Decimal | None:
    """The list cost the service reported, in dollars."""
    cost = (usage or {}).get("list_cost")
    return Decimal(str(cost)) if isinstance(cost, int | float) else None


class FindWorker:
    """Works finds on the deep service `deep`, reading papers' texts from `archive`."""

    def __init__(self, db: DatabaseGate, archive: ArchiveClient, deep: Deep) -> None:
        self.db, self.archive, self.deep = db, archive, deep

    async def __call__(self, item: WorkItem) -> Done | Later:
        paper_id = item.scope["paper_id"]
        async with self.db.write("find links: look") as unit:
            if kept := await work.results(unit, item.id):  # by a try whose end was lost
                body = kept[-1][1]
                if "failed" in body:
                    raise FindError(body["failed"])
                return Done(_cost(body.get("usage")), body)
            of = await store.identity(unit, paper_id)
            paper = await papers_store.get(unit, paper_id)
        if of is None or paper is None or of.dropped:
            return Done(result=links.SKIPPED)
        if of.find != item.scope["find"]:
            return Done(result=links.SUPERSEDED)
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
        if found.unsettled:
            log.warning("find links %s: %s; followed again", paper_id, found.error)
            return Later(datetime.now(UTC) + PAUSE)
        cost = _cost(found.usage)
        if found.answer is None:
            error = found.error or "the find gave no answer"
            async with self.db.write("record a failed find") as unit:
                failed = {"failed": error, "model": found.model, "usage": found.usage}
                await work.put_result(unit, item, failed)
            raise FindError(error)
        result = {"links": len(found.answer.links), "model": found.model, "usage": found.usage}
        async with self.db.write("record release links") as unit:
            ended = await links.record(unit, item, found.answer, result)
        return Done(cost, ended)


class RecheckWorker:
    """Works re-checks of placeholder links."""

    def __init__(self, db: DatabaseGate) -> None:
        self.db = db

    async def __call__(self, item: WorkItem) -> None:
        link_id, check = item.scope["link_id"], item.scope["check"]

        def due(link: ReleaseLink) -> bool:  # this check not counted already
            return link.rechecks == check - 1 and rechecked_again(link.state, link.rechecks)

        async with self.db.read("re-check a link: look") as unit:
            link = await store.get(unit, link_id)
        if link is None or not due(link):
            return
        checked = await adapter.check(link.url, link.kind)
        at = datetime.now(UTC)
        async with self.db.write("re-check a link") as unit:
            link = await store.locked(unit, link_id)
            if link is None or not due(link):
                return
            link = await store.rechecked(unit, link_id, checked, at)
            if rechecked_again(link.state, link.rechecks):
                await store.submit_recheck(unit, link, check + 1, at + RECHECK_EVERY)
