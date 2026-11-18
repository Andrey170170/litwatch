"""Publication lookup: library papers when saved and monthly after, and a single paper when
its BibTeX is copied (spec section 3, "Continuously")."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from litwatch import events
from litwatch.core import Publication
from litwatch.gates.database import DatabaseGate
from litwatch.jobs.context import run_record
from litwatch.publications.dblp import PublicationSource, SourceError
from litwatch.store import papers as papers_store
from litwatch.store import publications as store

log = logging.getLogger(__name__)

LIMIT = 200  # papers per run, a few minutes at DBLP's pace
GIVE_UP_AFTER = 3  # consecutive failures that end a run: the endpoint is down
FRESH = timedelta(days=1)  # a copy of one paper's BibTeX reuses a lookup this recent
# How long Copy BibTeX waits on DBLP, waiting for a scheduled lookup's query included,
# before it gives what is stored.
COPY_DEADLINE = 8.0


async def store_record(
    db: DatabaseGate,
    paper_id: str,
    record: Publication | None,
    *,
    by_owner: bool = False,
) -> bool:
    """Store a record, with `publication.found` when it is new for the paper, in a unit holding
    the paper's publication lock. Returns whether it was stored."""
    lock = ("publication", paper_id)
    async with db.write("store publication", locks=[lock]) as unit:
        stored, before = await store.put(unit, paper_id, record, by_owner=by_owner)
        if stored and record and record != before:
            await events.emit(
                unit,
                events.PUBLICATION_FOUND,
                paper_id,
                dblp_key=record.dblp_key,
                venue=record.venue,
                year=record.year,
                doi=record.doi,
                by_owner=by_owner,
            )
    return stored


async def look_up(
    db: DatabaseGate,
    source: PublicationSource,
    paper_id: str,
    deadline: float | None = None,
) -> Publication | None:
    """Ask the source and store its answer, unless the owner has chosen. Raises SourceError
    when the source cannot be asked, or does not answer within `deadline` seconds."""
    async with db.read("publication lookup: paper") as unit:
        paper = await papers_store.get(unit, paper_id)
    try:
        async with asyncio.timeout(deadline):
            record = await source.find(
                paper_id, paper.title if paper else None, paper.doi if paper else None
            )
    except TimeoutError as exc:
        raise SourceError(f"no answer within {deadline} s") from exc
    await store_record(db, paper_id, record)
    return record


async def for_copy(
    db: DatabaseGate, source: PublicationSource, paper_id: str
) -> Publication | None:
    """The record to cite one paper by: looked up first unless the owner chose or a lookup
    is recent. When the source cannot be asked, what is stored."""
    async with db.read("publication for a copy") as unit:
        row = await store.get(unit, paper_id)
    stale = row is None or (
        not row["by_owner"]
        and row["record"] is None
        and row["checked_at"] < datetime.now(UTC) - FRESH
    )
    if stale:
        try:
            return await look_up(db, source, paper_id, COPY_DEADLINE)
        except SourceError as exc:
            log.warning("publication lookup for %s failed: %s", paper_id, exc)
    async with db.read("publication stored") as unit:
        return (await store.found(unit, [paper_id])).get(paper_id)


async def run(db: DatabaseGate, source: PublicationSource, limit: int = LIMIT) -> dict[str, int]:
    """Look up the library papers that are due, as a `publications` run. Runs never overlap:
    each takes its due papers once, so one that died beside another would go unseen. The
    caller holds the schedule lock for `publications`: the scheduler, or the CLI."""
    async with run_record(db, "publications") as record:
        record.fields["cost"] = 0
        counts = record.counts
        counts |= {"looked_up": 0, "found": 0, "failed": 0}
        failures = 0
        async with db.read("publications due") as unit:
            due = await store.due(unit, limit)
        for paper_id in due:
            try:
                found = await look_up(db, source, paper_id)
            except SourceError as exc:
                counts["failed"] += 1
                failures += 1
                log.warning("publication lookup for %s failed: %s", paper_id, exc)
                if failures >= GIVE_UP_AFTER:
                    record.fields["error"] = f"stopped after {failures} failures in a row: {exc}"
                    break
                continue
            failures = 0
            counts["looked_up"] += 1
            counts["found"] += found is not None
    return counts
