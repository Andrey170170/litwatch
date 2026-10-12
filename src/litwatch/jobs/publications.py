"""Publication lookup: library papers when saved and monthly after, and a single paper when
its BibTeX is copied (spec section 3, "Continuously")."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import events
from litwatch.core import Publication
from litwatch.publications.dblp import PublicationSource, SourceError
from litwatch.store import papers as papers_store
from litwatch.store import publications as store
from litwatch.store import runs

log = logging.getLogger(__name__)

LIMIT = 200  # papers per run, a few minutes at DBLP's pace
GIVE_UP_AFTER = 3  # consecutive failures that end a run: the endpoint is down
FRESH = timedelta(days=1)  # a copy of one paper's BibTeX reuses a lookup this recent


async def store_record(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    record: Publication | None,
    *,
    by_owner: bool = False,
) -> bool:
    """Store a record, with `publication.found` when it is new for the paper. Returns
    whether it was stored."""
    async with conn.transaction():
        stored, before = await store.put(conn, paper_id, record, by_owner=by_owner)
        if stored and record and record != before:
            await events.emit(
                conn,
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
    conn: psycopg.AsyncConnection, source: PublicationSource, paper_id: str
) -> Publication | None:
    """Ask the source and store its answer, unless the owner has chosen. Raises SourceError
    when the source cannot be asked."""
    paper = await papers_store.get(conn, paper_id)
    record = await source.find(
        paper_id, paper.title if paper else None, paper.doi if paper else None
    )
    await store_record(conn, paper_id, record)
    return record


async def for_copy(
    conn: psycopg.AsyncConnection, source: PublicationSource, paper_id: str
) -> Publication | None:
    """The record to cite one paper by: looked up first unless the owner chose or a lookup
    is recent. When the source cannot be asked, what is stored."""
    row = await store.get(conn, paper_id)
    stale = row is None or (
        not row["by_owner"]
        and row["record"] is None
        and row["checked_at"] < datetime.now(UTC) - FRESH
    )
    if stale:
        try:
            return await look_up(conn, source, paper_id)
        except SourceError as exc:
            log.warning("publication lookup for %s failed: %s", paper_id, exc)
    return (await store.found(conn, [paper_id])).get(paper_id)


async def run(
    conn: psycopg.AsyncConnection, source: PublicationSource, limit: int = LIMIT
) -> dict[str, int]:
    """Look up the library papers that are due, as a `publications` run."""
    run_id = await runs.start(conn, "publications")
    counts = {"looked_up": 0, "found": 0, "failed": 0}
    failures, error = 0, None
    try:
        for paper_id in await store.due(conn, limit):
            try:
                record = await look_up(conn, source, paper_id)
            except SourceError as exc:
                counts["failed"] += 1
                failures += 1
                log.warning("publication lookup for %s failed: %s", paper_id, exc)
                if failures >= GIVE_UP_AFTER:
                    error = f"stopped after {failures} failures in a row: {exc}"
                    break
                continue
            failures = 0
            counts["looked_up"] += 1
            counts["found"] += record is not None
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        await runs.finish(conn, run_id, cost=0, counts=counts, error=error)
    return counts
