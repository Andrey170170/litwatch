"""The owner's word and what follows from it.

Every change that can move a paper (a verdict, a Weight, a label, a thumb, the Save toggle, an
edit of the followed topics) ends in `settle`, which recomputes the paper's effective Fit and
grade per followed topic, its rarity and whether it is in the library, stores them, and emits
`grade.changed`, `paper.saved` and `paper.unsaved` in the same transaction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from litwatch import events
from litwatch.core import (
    Depth,
    Effective,
    Grade,
    Hand,
    Label,
    Reaction,
    Status,
    Taxonomy,
    current_verdict,
    effective,
    in_library,
    mixed_rarity,
    owners_word,
)
from litwatch.store import analyses as analyses_store
from litwatch.store import feedback as feedback_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts as verdicts_store
from litwatch.store import word as word_store

log = logging.getLogger(__name__)
SYNC_PROGRESS = 2000  # `sync` logs its progress every this many papers


@dataclass
class Settled:
    """What a settle changed: effective grades as (before, after) per topic, and the library."""

    paper_id: str
    grades: dict[str, tuple[Grade | None, Grade | None]] = field(default_factory=dict)
    was_saved: bool = False
    saved: bool = False


# Advisory lock namespace for settling one paper; the second key is the paper id's hash.
SETTLE_LOCK = 7213912


async def lock(conn: psycopg.AsyncConnection, paper_id: str) -> None:
    """Hold the paper's settle lock to the end of the transaction. Every write that ends in a
    settle takes it first, so no two transactions wait on each other's rows.

    The taxonomy lock is taken shared first: a taxonomy edit takes it exclusively before any
    paper, so locks are always taken taxonomy first, and a settle never reads a depth that an
    edit in flight is about to change.
    """
    await taxonomy_store.lock_shared(conn)
    await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (SETTLE_LOCK, paper_id))


def _value(e: Effective | None) -> dict[str, str | None]:
    return {"grade": e.grade.value if e else None, "fit": e.fit.value if e else None}


async def settle(conn: psycopg.AsyncConnection, paper_id: str) -> Settled:
    """Recompute and store the paper's effective Fits and grades, its rarity and its library
    state, with their events.

    Settles of one paper are serialized and read only after the lock is held, so the last to
    run sees every committed change and leaves the stored state right.
    """

    async with conn.transaction():
        await lock(conn, paper_id)
        # Followed topics in the owner's order; a draft topic is not used.
        topics = {t.slug: t for t in await taxonomy_store.topics(conn) if t.status != Status.DRAFT}
        labels = {x.topic: x for x in await feedback_store.labels_for(conn, paper_id)}
        reactions = {r.topic: r for r in await word_store.reactions_for(conn, paper_id)}
        by_topic: dict[str, list] = {}
        for v in await verdicts_store.for_paper(conn, paper_id):
            by_topic.setdefault(v.topic, []).append(v)
        weight = await analyses_store.current_weight(conn, paper_id)
        before = await word_store.effective_for(conn, paper_id)

        result = Settled(paper_id)
        after: dict[str, Effective] = {}
        # A stored grade for a topic no longer followed is removed, with its event.
        involved = (set(topics) & (set(labels) | set(reactions) | set(by_topic))) | set(before)
        for topic in sorted(involved):
            verdict = current_verdict(by_topic.get(topic, []))
            new = None
            if t := topics.get(topic):
                fit = verdict.fit if verdict else None
                focus = t.depth == Depth.FOCUS
                word = owners_word(labels.get(topic), reactions.get(topic), fit, focus=focus)
                new = effective(word, fit, weight)
            if new is not None:
                after[topic] = new
            old = before.get(topic)
            if new == old:
                continue
            await word_store.set_effective(conn, paper_id, topic, new)
            if _value(new) != _value(old):
                result.grades[topic] = (old.grade if old else None, new.grade if new else None)
                b, a = _value(old), _value(new)
                await events.emit(
                    conn,
                    events.GRADE_CHANGED,
                    paper_id,
                    topic=topic,
                    before=b["grade"],
                    after=a["grade"],
                    fit_before=b["fit"],
                    fit_after=a["fit"],
                )

        # Rarity: the followed topics' page levels in the owner's order, lowered by depth.
        levels = [(s, t.depth, after[s].rarity) for s, t in topics.items() if s in after]
        rarity = mixed_rarity(levels, weight)
        if rarity != await word_store.rarity_of(conn, paper_id):
            await word_store.set_rarity(conn, paper_id, *rarity)

        row = await word_store.library_row(conn, paper_id)
        hand = Hand(row["hand"]) if row and row["hand"] else None
        result.was_saved = bool(row and row["saved"])
        focus_grades = [e.grade for s, e in after.items() if topics[s].depth == Depth.FOCUS]
        result.saved = in_library(hand, focus_grades)
        if result.saved != result.was_saved:
            await word_store.set_saved(conn, paper_id, result.saved)
            await events.emit(
                conn, events.PAPER_SAVED if result.saved else events.PAPER_UNSAVED, paper_id
            )
    return result


async def label(conn: psycopg.AsyncConnection, given: Label) -> tuple[Label, Settled]:
    async with conn.transaction():
        await lock(conn, given.paper_id)
        stored = await feedback_store.set_label(conn, given)
        return stored, await settle(conn, given.paper_id)


async def unlabel(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> Settled:
    async with conn.transaction():
        await lock(conn, paper_id)
        await feedback_store.delete_label(conn, paper_id, topic)
        return await settle(conn, paper_id)


async def react(conn: psycopg.AsyncConnection, given: Reaction) -> tuple[Reaction, Settled]:
    async with conn.transaction():
        await lock(conn, given.paper_id)
        stored = await word_store.set_reaction(conn, given)
        return stored, await settle(conn, given.paper_id)


async def unreact(conn: psycopg.AsyncConnection, paper_id: str, topic: str | None) -> Settled:
    """Clear a thumb. It takes back its own word and nothing else."""
    async with conn.transaction():
        await lock(conn, paper_id)
        await word_store.clear_reaction(conn, paper_id, topic)
        return await settle(conn, paper_id)


async def toggle(conn: psycopg.AsyncConnection, paper_id: str, hand: Hand) -> Settled:
    """Save or unsave by hand. The newest use outranks every other reason, until the next."""
    async with conn.transaction():
        await lock(conn, paper_id)
        await word_store.set_hand(conn, paper_id, hand)
        return await settle(conn, paper_id)


async def save_taxonomy(conn: psycopg.AsyncConnection, tree: Taxonomy) -> taxonomy_store.Saved:
    """Save the taxonomy and, in the same transaction, settle the papers of every topic it
    changed: a topic no longer followed takes its grades with it, a new depth moves its
    papers' rarity, and one no longer at Focus its papers' place in the library."""
    async with conn.transaction():
        saved = await taxonomy_store.save(conn, tree)
        for slug in saved.changed_topics:
            await settle_topic(conn, slug)
    return saved


async def settle_topic(conn: psycopg.AsyncConnection, slug: str) -> int:
    """Settle every paper the topic has a grade, verdict or word for, in id order. Call it in
    the transaction that changed the topic, after `taxonomy.lock`."""
    rows = await (
        await conn.execute(
            "SELECT paper_id FROM effective_grades WHERE topic = %(t)s "
            "UNION SELECT paper_id FROM verdicts WHERE topic = %(t)s "
            "UNION SELECT paper_id FROM labels WHERE topic = %(t)s "
            "UNION SELECT paper_id FROM reactions WHERE topic = %(t)s ORDER BY 1",
            {"t": slug},
        )
    ).fetchall()
    for row in rows:
        await settle(conn, row["paper_id"])
    return len(rows)


async def sync(conn: psycopg.AsyncConnection) -> dict[str, int]:
    """Settle every paper that has a verdict, a word or a library row: after anything that
    wrote verdicts or labels around `settle`."""
    counts = {"papers": 0, "grades_changed": 0, "saved": 0, "unsaved": 0}
    touched = await word_store.touched(conn)
    for paper_id in touched:
        settled = await settle(conn, paper_id)
        counts["papers"] += 1
        if counts["papers"] % SYNC_PROGRESS == 0:
            log.info("settled %s of %s papers", counts["papers"], len(touched))
        counts["grades_changed"] += len(settled.grades)
        if settled.saved != settled.was_saved:
            counts["saved" if settled.saved else "unsaved"] += 1
    return counts
