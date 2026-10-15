"""The second intake source: the papers of a date window, read from the archive instead of
arXiv. The restart, the sandbox and a backfill use it.

The window is the archive's: papers whose first version was submitted in it, in the
categories asked for (cross-lists included). Papers are stored as the arXiv source stores
them, with source "archive": new ones are inserted, newer versions applied as revisions.
Their events say so, and the archive integration does not register them again.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date

import psycopg

from litwatch.archive import ArchiveClient
from litwatch.intake.collect import IntakeResult
from litwatch.store import papers as papers_store

log = logging.getLogger(__name__)


async def collect(
    conn: psycopg.AsyncConnection,
    client: ArchiveClient,
    categories: Iterable[str],
    since: date,
    until: date,
) -> IntakeResult:
    """Store the window's papers a page at a time, each page in its own transaction, so an
    interrupted read keeps what it stored and a repeat changes nothing. A record without a
    title or abstract is skipped: there is nothing to judge."""
    categories = tuple(dict.fromkeys(categories))
    result = IntakeResult(
        details={
            "status": "complete",
            "source": papers_store.ARCHIVE,
            "since": since.isoformat(),
            "until": until.isoformat(),
            "categories": list(categories),
            "skipped": [],
        }
    )
    seen: set[str] = set()
    async for page in client.papers(since, until, categories):
        usable = [p for p in page if p.title and p.abstract]
        result.details["skipped"] += [p.id for p in page if not (p.title and p.abstract)]
        stored = await papers_store.upsert(conn, usable, source=papers_store.ARCHIVE)
        result.inserted += stored.inserted
        result.updated += stored.updated
        result.rewritten += stored.rewritten
        seen.update(p.id for p in usable)
    result.fetched = len(seen)
    if result.details["skipped"]:
        log.warning("archive intake skipped %s papers without text", len(result.details["skipped"]))
    return result
