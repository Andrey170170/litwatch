"""Looking a paper up by its arXiv link, and reporting it as a miss for a topic.

Filing a miss is the owner's word (a label of central or related, origin `miss`), a membership
of the topic's class (source `miss`, so grading for the topic runs whatever classification
says), and the owner's newest Save (feedback.md, misses).
"""

from __future__ import annotations

import psycopg

from litwatch.core import Fit, Hand, Label, LabelOrigin, LabelValue, Paper
from litwatch.core.classification import Source
from litwatch.feedback import word
from litwatch.feedback.word import Change
from litwatch.intake import batch
from litwatch.intake.search import fetch_by_ids, id_from_url
from litwatch.store import classifications as classifications_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import word as word_store


class NotFoundError(LookupError):
    """The text names no arXiv paper, or arXiv does not know it."""


SILENT = "arXiv did not answer in time, and nothing was stored; try again in a minute"


async def lookup(conn: psycopg.AsyncConnection, text: str) -> tuple[Paper, bool]:
    """The paper an arXiv id or link names, fetched and stored when the collection lacks it.
    Returns the paper and whether it was fetched just now."""
    return await keep(conn, *await resolve(conn, text))


async def resolve(conn: psycopg.AsyncConnection, text: str) -> tuple[str, Paper | None]:
    """The arXiv id `text` names, and arXiv's record of the paper when litwatch lacks it. Call
    it outside a transaction: arXiv may keep it waiting for `search.LOOKUP_BUDGET`. NotFoundError
    when the text names no paper or arXiv does not know it; NoAnswerError when arXiv gives no
    answer in that time."""
    try:
        paper_id = id_from_url(text)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    if await papers_store.get(conn, paper_id):
        return paper_id, None
    fetched = await fetch_by_ids([paper_id])
    if not fetched:
        raise NotFoundError(f"arXiv has no paper {paper_id}")
    return paper_id, fetched[0]


async def keep(
    conn: psycopg.AsyncConnection, paper_id: str, fetched: Paper | None
) -> tuple[Paper, bool]:
    """Store the record `resolve` fetched, through intake's writer (`batch.store`): the paper's
    settle lock first, so a request or an intake storing it meanwhile goes first and this one
    stores over it as intake would, settling a rewrite. Returns the paper and whether it was
    inserted just now."""
    if fetched is not None:
        stored = await batch.store(conn, [fetched], source="arxiv")
    paper = await papers_store.get(conn, paper_id)
    assert paper is not None  # stored now, or held when resolve looked
    return paper, fetched is not None and stored.inserted > 0


async def report(
    conn: psycopg.AsyncConnection, paper_id: str, topic: str, fit: Fit = Fit.CENTRAL
) -> tuple[int, Change, bool]:
    """File the miss and give the owner's word on it: `fit` (central or related) for the topic,
    a membership of the topic's class with source `miss`, and the owner's newest Save, as a
    change of the paper (`word.change`). Returns the miss id, the change and whether the miss
    is new. Reporting again keeps the miss and saves again: the owner's word is given again only
    where none stands (it was taken back), and never over a label the owner gave since. Raises
    ValueError for a Fit below related."""
    if fit not in (Fit.CENTRAL, Fit.RELATED):
        raise ValueError("a miss is central or related")
    word_on_it = Label(
        paper_id=paper_id, topic=topic, value=LabelValue(fit.value), origin=LabelOrigin.MISS
    )
    # The paper's lock first, so looking for a standing label and giving the word again are one
    # step against any label being written meanwhile.
    async with word.change(conn, paper_id) as done:
        row = await (
            await conn.execute(
                "INSERT INTO misses (paper_id, topic) VALUES (%s, %s) "
                "ON CONFLICT (paper_id, topic) DO NOTHING RETURNING id",
                (paper_id, topic),
            )
        ).fetchone()
        await word_store.set_hand(conn, paper_id, Hand.SAVE)
        tree = await taxonomy_store.load(conn)
        paper = await papers_store.get(conn, paper_id)
        if tree is not None and paper is not None and topic in {c.slug for c in tree.classes}:
            version = tree.get(topic).version
            await classifications_store.put_owner(
                conn, paper, topic, True, version, source=Source.MISS
            )
        if row is None:
            row = await (
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
            new = False
        else:
            labelled, new = None, True
        if not labelled:
            await word.label(conn, word_on_it)
    return row["id"], done, new
