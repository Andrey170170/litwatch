"""Storing what a source read: the papers, and what follows from a rewrite, committed together.

Every intake source stores through `store`, so no caller has to remember to settle what a
revision rewrote, and no failure or interruption can leave a stored rewrite unsettled.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import psycopg

from litwatch.core import Paper
from litwatch.feedback import word
from litwatch.store import papers as papers_store

# The most papers one transaction stores, and so the most settle locks it holds at once.
PAGE = 500


async def store(
    conn: psycopg.AsyncConnection,
    papers: list[Paper],
    *,
    source: str,
    listed: Mapping[str, date] | None = None,
) -> papers_store.Upserted:
    """Store `papers` (`store.papers.upsert`) a page at a time, each page in a transaction of
    its own: the page's settle locks first (`word.lock_many`), then the papers, then a settle
    of each one rewritten. A rewrite's Weight went with the old text, so its grades fall back
    to solid until the new text is analysed, and a new arXiv version queues the passes that
    read the text again."""
    total = papers_store.Upserted()
    for start in range(0, len(papers), PAGE):
        page = papers[start : start + PAGE]
        async with conn.transaction():
            await word.lock_many(conn, [p.id for p in page])
            stored = await papers_store.upsert(conn, page, source=source, listed=listed)
            await word.settle_all(conn, stored.rewritten)
        total.inserted += stored.inserted
        total.updated += stored.updated
        total.rewritten += stored.rewritten
    return total
