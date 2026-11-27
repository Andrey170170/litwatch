"""Storing what a source read: the papers, and what follows from a rewrite, committed together.

Every intake source stores through `store`, so no caller has to remember to settle what a
revision rewrote, and no failure or interruption can leave a stored rewrite unsettled.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from litwatch import links
from litwatch.core import Cause, Paper
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate
from litwatch.store import fulltext as fulltext_store
from litwatch.store import papers as papers_store

# The most papers one unit stores, and so the most settle locks it holds at once.
PAGE = 500


async def store(
    db: DatabaseGate,
    papers: list[Paper],
    *,
    source: str,
    rules: word.Rules,
    listed: Mapping[str, date] | None = None,
) -> papers_store.Upserted:
    """Store `papers` (`store.papers.upsert`) a page at a time, each page in a write unit of
    its own that holds the page's settle locks: the papers, then a settle of each one
    rewritten, withdrawn or brought back, by `rules`. A rewrite's Weight went with the old
    text, so its grades fall back to solid until the new text is analysed, and a new arXiv
    version queues the passes that read the text again, and a paper brought back has its open
    passes run again (`store.fulltext.submit_restored`), and a new version's release links are
    found again (`links.due`). A paper fetched inside a change of it
    is stored there with `store.papers.upsert` instead, and that change settles it."""
    total = papers_store.Upserted()
    for start in range(0, len(papers), PAGE):
        page = papers[start : start + PAGE]
        ids = [p.id for p in page]
        async with db.write("store papers", taxonomy="shared", papers=ids) as unit:
            stored = await papers_store.upsert(unit, page, source=source, listed=listed)
            settled = list(dict.fromkeys(stored.rewritten + stored.withdrawn))
            await word.settle_many(unit, settled, cause=Cause.VERDICT, rules=rules)
            await fulltext_store.submit_restored(unit, stored.withdrawn)
            if rules.find_links:
                await links.due(unit, stored.versioned)
        total.inserted += stored.inserted
        total.updated += stored.updated
        total.rewritten += stored.rewritten
    return total
