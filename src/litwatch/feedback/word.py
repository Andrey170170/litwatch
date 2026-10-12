"""The owner's word and what follows from it.

Every change that can move a paper (a verdict, a label, a thumb, the Save toggle) ends in
`settle`, which recomputes the paper's effective grade per Watch topic and whether it is in
the library, stores both, and emits `grade.changed`, `paper.saved` and `paper.unsaved` in the
same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from litwatch import events
from litwatch.core import (
    Grade,
    Hand,
    Label,
    Reaction,
    TopicKind,
    current_verdict,
    effective_grade,
    in_library,
    owners_word,
)
from litwatch.store import feedback as feedback_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts as verdicts_store
from litwatch.store import word as word_store


@dataclass
class Settled:
    """What a settle changed: effective grades as (before, after) per topic, and the library."""

    paper_id: str
    grades: dict[str, tuple[Grade | None, Grade | None]] = field(default_factory=dict)
    was_saved: bool = False
    saved: bool = False


async def _watch_topics(conn: psycopg.AsyncConnection) -> set[str]:
    return {t.slug for t in await topics_store.all(conn, None) if t.kind == TopicKind.WATCH}


# Advisory lock namespace for settling one paper; the second key is the paper id's hash.
SETTLE_LOCK = 7213911


async def settle(conn: psycopg.AsyncConnection, paper_id: str) -> Settled:
    """Recompute and store the paper's effective grades and library state, with their events.

    Settles of one paper are serialized and read only after the lock is held, so the last to
    run sees every committed change and leaves the stored state right.
    """
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (SETTLE_LOCK, paper_id)
        )
        watched = await _watch_topics(conn)
        labels = {x.topic: x for x in await feedback_store.labels_for(conn, paper_id)}
        reactions = {r.topic: r for r in await word_store.reactions_for(conn, paper_id)}
        by_topic: dict[str, list] = {}
        for v in await verdicts_store.for_paper(conn, paper_id):
            by_topic.setdefault(v.topic, []).append(v)
        before = await word_store.effective_for(conn, paper_id)

        result = Settled(paper_id)
        after: dict[str, Grade | None] = {}
        # A stored grade for a topic that is no longer Watch is removed, with its event.
        involved = (watched & (set(labels) | set(reactions) | set(by_topic))) | set(before)
        for topic in sorted(involved):
            word = owners_word(labels.get(topic), reactions.get(topic))
            old = before.get(topic)
            verdict = current_verdict(by_topic.get(topic, []))
            new = effective_grade(word, verdict) if topic in watched else None
            after[topic] = new
            if new != old:
                result.grades[topic] = (old, new)
                await word_store.set_effective(conn, paper_id, topic, new)
                await events.emit(
                    conn,
                    events.GRADE_CHANGED,
                    paper_id,
                    topic=topic,
                    before=old.value if old else None,
                    after=new.value if new else None,
                )

        row = await word_store.library_row(conn, paper_id)
        hand = Hand(row["hand"]) if row and row["hand"] else None
        result.was_saved = bool(row and row["saved"])
        result.saved = in_library(hand, after.values())
        if result.saved != result.was_saved:
            await word_store.set_saved(conn, paper_id, result.saved)
            await events.emit(
                conn, events.PAPER_SAVED if result.saved else events.PAPER_UNSAVED, paper_id
            )
    return result


async def label(conn: psycopg.AsyncConnection, given: Label) -> tuple[Label, Settled]:
    async with conn.transaction():
        stored = await feedback_store.set_label(conn, given)
        return stored, await settle(conn, given.paper_id)


async def unlabel(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> Settled:
    async with conn.transaction():
        await feedback_store.delete_label(conn, paper_id, topic)
        return await settle(conn, paper_id)


async def react(conn: psycopg.AsyncConnection, given: Reaction) -> tuple[Reaction, Settled]:
    async with conn.transaction():
        stored = await word_store.set_reaction(conn, given)
        return stored, await settle(conn, given.paper_id)


async def unreact(conn: psycopg.AsyncConnection, paper_id: str, topic: str | None) -> Settled:
    """Clear a thumb. It takes back its own word and nothing else."""
    async with conn.transaction():
        await word_store.clear_reaction(conn, paper_id, topic)
        return await settle(conn, paper_id)


async def toggle(conn: psycopg.AsyncConnection, paper_id: str, hand: Hand) -> Settled:
    """Save or unsave by hand. The newest use outranks every other reason, until the next."""
    async with conn.transaction():
        await word_store.set_hand(conn, paper_id, hand)
        return await settle(conn, paper_id)


async def sync(conn: psycopg.AsyncConnection) -> dict[str, int]:
    """Settle every paper that has a verdict, a word or a library row. For an adopted or
    seeded database, and after anything that wrote verdicts or labels around `settle`."""
    counts = {"papers": 0, "grades_changed": 0, "saved": 0, "unsaved": 0}
    for paper_id in await word_store.touched(conn):
        settled = await settle(conn, paper_id)
        counts["papers"] += 1
        counts["grades_changed"] += len(settled.grades)
        if settled.saved != settled.was_saved:
            counts["saved" if settled.saved else "unsaved"] += 1
    return counts
