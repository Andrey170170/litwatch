"""The Workers of release links (docs/plans/a2-work-gate.md, P6): a find of one paper's links on
the deep service (`FindWorker`, items of kind `store.links.FIND`) and a re-check of one
placeholder link at its host (`RecheckWorker`, kind `store.links.RECHECK`).

Each find ends with one record, its item's result (`links.record`, `links.check`): answered,
failed, superseded, dropped or answered by another, with the usage the service reported, written
in the unit, holding the paper's locks, that replaces the paper's links or hands the find over.
A try of an item with its record ends by it before anything else, so a find is never paid for
twice nor handed over twice (its item's end, a commit of the Work gate's own after the Worker's,
lost when its process died). One without asks deep about its key first: a find a try before
started (its answer lost, its end not known) is followed to its end and recorded as any, an
answer for what the paper no longer is ending superseded with its cost. Only a key deep does not
know is sent: the paper's text read first from the archive, waiting for it a while as a Deep
pass does, then without it (the finder starts from the abstract and arXiv's comments), and
right before it pays, after any wait, the find asked whether it is still to be asked
(`links.check`). A failed find fails its item with its cost (`FindError`); a later trigger
submits a fresh one (`links.due`).

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
from litwatch.core import Paper
from litwatch.core.fulltext import TEXT_WAIT
from litwatch.core.links import RECHECK_EVERY, ReleaseLink, rechecked_again
from litwatch.core.work import WorkItem
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import Done, Later, TerminalError
from litwatch.integrations.deep import Deep, Found, RejectedError, find_input
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


class FindError(TerminalError):
    """A find that ended without an answer, with what it cost: tried again by its key it would
    end so again."""


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
        async with self.db.read("find links: look") as unit:
            if kept := await work.results(unit, item.id):  # by a try whose end was lost
                return _replayed(kept[-1][1])
            paper = await papers_store.get(unit, paper_id)
        if paper is None:
            return Done(result=links.SKIPPED)
        key = f"links:{item.id}:{paper_id}"
        try:
            # A find a try before started (its answer lost, its end not known) is followed, so
            # what it spent is recorded and its answer checked as any; only one deep does not
            # know is sent.
            started = await self.deep.client.find_for(key) or await self._start(item, paper, key)
            if isinstance(started, Done | Later):
                return started
            if isinstance(started, str):
                started = await self.deep.client.follow_find(started)
            found = started
        except httpx.HTTPError as exc:  # asked again by its key, it is the same find
            log.warning("find links %s: the deep service: %s", paper_id, exc)
            return Later(datetime.now(UTC) + PAUSE)
        if found.unsettled:
            log.warning("find links %s: %s; followed again", paper_id, found.error)
            return Later(datetime.now(UTC) + PAUSE)
        paid = {"model": found.model, "usage": found.usage}
        if found.answer is None:
            result = {"failed": found.error or "the find gave no answer"} | paid
        else:
            result = {"links": len(found.answer.links)} | paid
        async with self.db.write("record a find", taxonomy="shared", papers=[paper_id]) as unit:
            kept = await links.record(unit, item, found.answer, result)
        return _replayed(kept)  # committed first: a failure raises after its record

    async def _start(self, item: WorkItem, paper: Paper, key: str) -> str | Found | Done | Later:
        """Read the paper's text, waiting for it a while, ask whether the find is still to be
        asked (`links.check`), right before it pays, after any wait, and send it: the find's
        id, how it ended when deep turned it down, or how the try ends instead."""
        text: PaperText | None = None
        try:
            text = await read_text(self.archive, paper, URGENT)
        except ArchiveUnavailable as exc:
            log.info("find links %s: the archive: %s", paper.id, exc)
            if item.attempts < TEXT_TRIES:
                return Later(datetime.now(UTC) + TEXT_WAIT)
        except ArchiveError as exc:  # no text to be had: the finder starts from the abstract
            log.info("find links %s: no text: %s", paper.id, exc)
        else:
            if text is None and item.attempts < TEXT_TRIES:  # the archive is fetching it
                return Later(datetime.now(UTC) + TEXT_WAIT)
        async with self.db.write("find links: check", taxonomy="shared", papers=[paper.id]) as unit:
            ended = await links.check(unit, item)
            now = await papers_store.get(unit, paper.id)
        if ended is not None:
            return _replayed(ended)
        assert now is not None  # held: `check` found it
        try:
            return await self.deep.client.find(find_input(now, text), key=key)
        except RejectedError as exc:  # turned down for what it is: sent again, so again
            return Found(None, error=f"the deep service refused the find: {exc}", failure="paper")


def _replayed(kept: dict[str, Any]) -> Done:
    """How a find ends by its record (`links.record`, `links.check`), with what it spent: a
    failure raises `FindError`."""
    cost = _cost(kept.get("usage"))
    if "failed" in kept:
        raise FindError(kept["failed"], cost)
    return Done(cost, kept)


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
