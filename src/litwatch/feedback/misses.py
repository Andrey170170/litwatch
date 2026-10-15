"""Looking a paper up by its arXiv link, and reporting it as a miss for a Watch topic.

Filing a miss is the owner's word (a label of relevant, origin `miss`), so it saves the paper
like any label.
"""

from __future__ import annotations

import asyncio

import psycopg

from litwatch.core import Label, LabelOrigin, LabelValue, Paper
from litwatch.feedback import word
from litwatch.feedback.word import Settled
from litwatch.intake.search import fetch_by_ids, id_from_url
from litwatch.store import papers as papers_store


class NotFoundError(LookupError):
    """The text names no arXiv paper, or arXiv does not know it."""


async def lookup(conn: psycopg.AsyncConnection, text: str) -> tuple[Paper, bool]:
    """The paper an arXiv id or link names, fetched and stored when the collection lacks it.
    Returns the paper and whether it was fetched just now."""
    try:
        paper_id = id_from_url(text)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    if stored := await papers_store.get(conn, paper_id):
        return stored, False
    fetched = await asyncio.to_thread(fetch_by_ids, [paper_id])
    if not fetched:
        raise NotFoundError(f"arXiv has no paper {paper_id}")
    await papers_store.upsert(conn, fetched)
    return await papers_store.get(conn, paper_id), True


async def report(
    conn: psycopg.AsyncConnection, paper_id: str, topic: str
) -> tuple[int, Settled, bool]:
    """File the miss and give the owner's word on it. Returns the miss id, what settled and
    whether it is new. Reporting again keeps the miss: the owner's word is given again only
    where none stands (it was taken back), and never over a label the owner gave since."""
    word_on_it = Label(
        paper_id=paper_id, topic=topic, value=LabelValue.RELEVANT, origin=LabelOrigin.MISS
    )
    async with conn.transaction():
        # The paper's lock first, so looking for a standing label and giving the word again
        # are one step against any label being written meanwhile.
        await word.lock(conn, paper_id)
        row = await (
            await conn.execute(
                "INSERT INTO misses (paper_id, topic) VALUES (%s, %s) "
                "ON CONFLICT (paper_id, topic) DO NOTHING RETURNING id",
                (paper_id, topic),
            )
        ).fetchone()
        if row is None:
            existing = await (
                await conn.execute(
                    "SELECT id FROM misses WHERE paper_id = %s AND topic = %s",
                    (paper_id, topic),
                )
            ).fetchone()
            labelled = await (
                await conn.execute(
                    "SELECT 1 FROM labels WHERE paper_id = %s AND topic = %s", (paper_id, topic)
                )
            ).fetchone()
            if labelled:
                settled = await word.settle(conn, paper_id)
            else:
                _, settled = await word.label(conn, word_on_it)
            return existing["id"], settled, False
        _, settled = await word.label(conn, word_on_it)
    return row["id"], settled, True
