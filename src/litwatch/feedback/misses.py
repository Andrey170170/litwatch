"""Looking a paper up by its arXiv link, and reporting it as a miss for a topic.

Filing a miss is the owner's word (a label of central or related, origin `miss`), a membership
of the topic's class (source `miss`, so grading for the topic runs whatever classification
says), and the owner's newest Save (feedback.md, misses).
"""

from __future__ import annotations

from litwatch import links
from litwatch.core import Fit, Hand, Label, LabelOrigin, LabelSetter, LabelValue, Paper
from litwatch.core.classification import Source
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate, WriteUnit
from litwatch.intake.search import fetch_by_ids, id_from_url
from litwatch.store import classifications as classifications_store
from litwatch.store import feedback as feedback_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import word as word_store


class NotFoundError(LookupError):
    """The text names no arXiv paper, or arXiv does not know it."""


SILENT = "arXiv did not answer in time, and nothing was stored; try again in a minute"


async def resolve(db: DatabaseGate, text: str) -> tuple[str, Paper | None]:
    """The arXiv id `text` names, and arXiv's record of the paper when litwatch lacks it, asked
    for with no unit open: arXiv may keep it waiting for `search.LOOKUP_BUDGET`. NotFoundError
    when the text names no paper or arXiv does not know it; NoAnswerError when arXiv gives no
    answer in that time."""
    try:
        paper_id = id_from_url(text)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    async with db.read("resolve paper") as unit:
        held = await papers_store.get(unit, paper_id)
    if held:
        return paper_id, None
    fetched = await fetch_by_ids([paper_id])
    if not fetched:
        raise NotFoundError(f"arXiv has no paper {paper_id}")
    return paper_id, fetched[0]


async def keep(
    unit: WriteUnit, paper_id: str, fetched: Paper | None, rules: word.Rules
) -> tuple[Paper, bool]:
    """Store the record `resolve` fetched, as intake stores one (`store.papers.upsert`), in a unit
    holding the paper's settle lock, so a request or an intake storing it meanwhile went
    first and this one stores over it as intake would; the unit settles the paper after it, a
    rewrite included (a change of the paper, `word.change`). A new version stored here has its
    release links found again, by `rules`, as intake's would (`links.stored`). Returns the
    paper and whether it was inserted just now."""
    if fetched is not None:
        stored = await papers_store.upsert(unit, [fetched], source="arxiv")
        await links.stored(unit, stored, find_links=rules.find_links)
    paper = await papers_store.get(unit, paper_id)
    assert paper is not None  # stored now, or held when resolve looked
    return paper, fetched is not None and stored.inserted > 0


async def report(
    unit: WriteUnit, paper_id: str, topic: str, fit: Fit = Fit.CENTRAL
) -> tuple[int, bool]:
    """File the miss and give the owner's word on it: `fit` (central or related) for the topic,
    a membership of the topic's class with source `miss`, and the owner's newest Save, in the
    unit of a change of the paper (`word.change`), which settles it. Returns the miss id and
    whether the miss is new. Reporting again keeps the miss and saves again: the owner's word is
    given again only where none stands (it was taken back), and never over a label the owner
    gave since. Raises ValueError for a Fit below related."""
    if fit not in (Fit.CENTRAL, Fit.RELATED):
        raise ValueError("a miss is central or related")
    word_on_it = Label(
        paper_id=paper_id,
        topic=topic,
        value=LabelValue(fit.value),
        origin=LabelOrigin.MISS,
        set_by=LabelSetter.MISS,
    )
    # Under the paper's lock, looking for a standing label and giving the word again are one
    # step against any label being written meanwhile.
    miss_id, new = await feedback_store.add_miss(unit, paper_id, topic)
    await word_store.set_hand(unit, paper_id, Hand.SAVE)
    tree = await taxonomy_store.load(unit)
    paper = await papers_store.get(unit, paper_id)
    if tree is not None and paper is not None and topic in {c.slug for c in tree.classes}:
        version = tree.get(topic).version
        await classifications_store.put_owner(unit, paper, topic, True, version, source=Source.MISS)
    if new or not await feedback_store.labelled(unit, paper_id, topic):
        await word.label(unit, word_on_it)
    return miss_id, new
