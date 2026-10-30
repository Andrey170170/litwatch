"""Intake's catalog pass: every new or revised arXiv paper outside intake's categories, registered
with the archive as metadata only, so the archive stays almost all of arXiv (pipeline.md,
section 1; decisions 88–90). Those papers are not stored in the collection, and no file is
asked for: the archive fetches one when the paper is read.

The pass harvests OAI-PMH with no set from its own checkpoint, the category `*` in
`intake_checkpoints`, a week at a time up to today, and registers each record with the
archive's `upsert_paper`, whose merge keeps the higher version and the newer metadata. A
record in any of intake's categories is its category pass's, and deleted records are skipped.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg

from litwatch.archive import ArchiveClient
from litwatch.intake import oai
from litwatch.intake.http import SourceError, XmlClient
from litwatch.intake.source import PaperSource
from litwatch.store import checkpoints

log = logging.getLogger(__name__)

WEEK = 7  # days in the widest window


async def lower(conn: psycopg.AsyncConnection, day: date) -> None:
    """Have the next pass start from `day` (`litwatch intake --catalog-from`), unless a pass was
    already asked from `day` or earlier: then it goes on from its checkpoint, so the same
    command resumes a backfill that stopped."""
    await checkpoints.lower(conn, oai.ALL, day)


async def run(
    conn: psycopg.AsyncConnection,
    archive: ArchiveClient,
    categories: Iterable[str],
    since: date,
    run_id: int,
    client: XmlClient,
    *,
    source: PaperSource = oai,
    report: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Harvest from the checkpoint (from `since` the first time) to today, window by window,
    and return what was done; never raises but for a cancellation, as a failed pass must not
    fail intake. The checkpoint moves after each complete window, its harvests and its
    registrations all done: to the window's last day, or for the open window that reaches
    today, to the day of the OAI response. A window that does not complete (over the harvest's
    budget, or with revisions it could not resolve) is halved, down to one day; a day that
    still does not complete stops the pass, to be harvested again from the checkpoint.
    `report` is called with the outcome after each window."""
    skip = set(categories)
    start = await checkpoints.start(conn, oai.ALL, since)
    info: dict[str, Any] = {
        "from": start.isoformat(),
        "complete": False,
        "windows": 0,
        "records": 0,
        "registered": 0,
    }
    width = WEEK
    try:
        while True:
            today = datetime.now(UTC).date()
            last = start + timedelta(days=width - 1)
            until = last if last < today else None
            span = ((until or today) - start).days + 1
            try:
                harvest = await asyncio.to_thread(
                    source.fetch_category,
                    oai.ALL,
                    start,
                    client,
                    include_versions=True,
                    until=until,
                )
                # A record without its version would not replace a seeded paper's.
                unfinished = harvest.versions_error and f"versions: {harvest.versions_error}"
            except oai.OverBudget as exc:
                unfinished = str(exc)
            if unfinished:
                if span > 1:
                    width = span // 2
                    continue
                raise SourceError(unfinished)
            if harvest.checkpoint.date() < start:
                raise SourceError("OAI response predates the requested coverage")
            if harvest.checkpoint > datetime.now(UTC) + timedelta(minutes=5):
                raise SourceError("OAI response timestamp is in the future")
            outside = [p for p in harvest.papers if skip.isdisjoint(p.categories)]
            for paper in outside:
                await archive.upsert_paper(paper)
            through = until or harvest.checkpoint.date()
            await checkpoints.advance(conn, oai.ALL, through, run_id)
            info["windows"] += 1
            info["records"] += len(harvest.papers)
            info["registered"] += len(outside)
            info["through"] = through.isoformat()
            if report:
                report(info)
            if until is None:
                info["complete"] = True
                return info
            start = until + timedelta(days=1)
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("catalog pass stopped from %s: %s", start, info["error"])
        return info
