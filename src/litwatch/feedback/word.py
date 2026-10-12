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
    Take,
    Topic,
    TopicKind,
    Verdict,
    current_verdict,
    effective_grade,
    in_library,
    owners_word,
)
from litwatch.store import digest as digest_store
from litwatch.store import feedback as feedback_store
from litwatch.store import papers as papers_store
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


async def _watch_topics(conn: psycopg.AsyncConnection) -> dict[str, Topic]:
    return {t.slug: t for t in await topics_store.all(conn, None) if t.kind == TopicKind.WATCH}


def _shown(grade: Grade | None) -> bool:
    return grade is not None and grade.at_least(Grade.RELEVANT)


def fits(take: Take | None, grade: Grade, binding: tuple[int, int]) -> bool:
    """Whether a take was written for this text and rubric (the binding) and this length."""
    return take is not None and (
        take.paper_revision,
        take.topic_revision,
        take.grade == Grade.MUST_READ,
    ) == (*binding, grade == Grade.MUST_READ)


async def wanted(
    conn: psycopg.AsyncConnection, paper_id: str, topic: Topic
) -> tuple[Grade, tuple[int, int]] | None:
    """The grade and binding a take for the pair must have now, or None when the pair is not
    shown. Read under the paper's lock to act on it."""
    grade = (await word_store.effective_for(conn, paper_id)).get(topic.slug)
    if topic.kind != TopicKind.WATCH or not _shown(grade):
        return None
    verdict = await verdicts_store.current(conn, paper_id, topic.slug)
    revision = await papers_store.revision(conn, paper_id)
    return grade, binding(verdict, revision, topic.revision or 1)


def speaks(verdict: Verdict | None) -> bool:
    """Whether the verdict itself puts the paper on the reading side. When only the owner's
    word does, the take is the owner's and the graders' opinions are not its source."""
    return verdict is not None and _shown(verdict.grade)


def binding(verdict: Verdict | None, paper_revision: int, topic_revision: int) -> tuple[int, int]:
    """What a take must be written from: always the paper's current text (only it is stored),
    and the rubric the verdict was given under when the verdict is the take's source."""
    return paper_revision, verdict.topic_revision if speaks(verdict) else topic_revision


async def _owe(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    topic: str,
    old: Grade | None,
    new: Grade | None,
    binding: tuple[int, int],
    announce: bool,
) -> None:
    """Keep the pair's debts to the writer and the next issue in step with its grade.

    Shown (relevant or above), the pair is owed a take unless its newest take was written for
    this text, rubric and length, and is owed an entry when it has just crossed the line and
    no issue has carried it yet. Falling below the line cancels both. A paper newly adjacent
    is counted in the next issue's fold line.
    """
    if _shown(new):
        stale = not fits(await digest_store.latest_take(conn, paper_id, topic), new, binding)
        crossed = (
            announce and not _shown(old) and not await digest_store.announced(conn, paper_id, topic)
        )
        if stale or crossed:
            await digest_store.mark_due(conn, paper_id, topic, take=stale, issue=crossed)
        if not stale:  # back to what the newest take was written for: no rewrite owed
            await digest_store.settle_due(conn, paper_id, topic, take=False)
        if not _shown(old):
            await digest_store.settle_due(conn, paper_id, topic, fold=False)
    elif new != old:
        await digest_store.clear_due(conn, paper_id, topic)
        if announce and new == Grade.ADJACENT and old in (None, Grade.NONE):
            await digest_store.mark_due(conn, paper_id, topic, fold=True)


# Advisory lock namespace for settling one paper; the second key is the paper id's hash.
SETTLE_LOCK = 7213912


async def lock(conn: psycopg.AsyncConnection, paper_id: str) -> None:
    """Hold the paper's settle lock to the end of the transaction. Every write that ends in a
    settle takes it first, so no two transactions wait on each other's rows.

    The topic rows are share-locked first, in slug order: a topic edit locks them all before
    any paper, so locks are always taken topics first, and a settle never reads a kind that
    an edit in flight is about to change.
    """
    await conn.execute("SELECT slug FROM topics ORDER BY slug FOR SHARE")
    await conn.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (SETTLE_LOCK, paper_id))


async def settle(conn: psycopg.AsyncConnection, paper_id: str, *, announce: bool = True) -> Settled:
    """Recompute and store the paper's effective grades and library state, with their events,
    and what the writer and the next issue owe it. `announce=False` settles without making
    anything due for an issue, for papers brought in rather than found (sync, migration).

    Settles of one paper are serialized and read only after the lock is held, so the last to
    run sees every committed change and leaves the stored state right.
    """

    async with conn.transaction():
        await lock(conn, paper_id)
        watched = await _watch_topics(conn)
        revision = await papers_store.revision(conn, paper_id)
        labels = {x.topic: x for x in await feedback_store.labels_for(conn, paper_id)}
        reactions = {r.topic: r for r in await word_store.reactions_for(conn, paper_id)}
        by_topic: dict[str, list] = {}
        for v in await verdicts_store.for_paper(conn, paper_id):
            by_topic.setdefault(v.topic, []).append(v)
        before = await word_store.effective_for(conn, paper_id)

        result = Settled(paper_id)
        after: dict[str, Grade | None] = {}
        # A stored grade for a topic that is no longer Watch is removed, with its event.
        involved = (set(watched) & (set(labels) | set(reactions) | set(by_topic))) | set(before)
        for topic in sorted(involved):
            word = owners_word(labels.get(topic), reactions.get(topic))
            old = before.get(topic)
            verdict = current_verdict(by_topic.get(topic, []))
            new = after[topic] = effective_grade(word, verdict) if topic in watched else None
            if topic in watched:
                bound = binding(verdict, revision, watched[topic].revision or 1)
                await _owe(conn, paper_id, topic, old, new, bound, announce)
            elif new != old:
                await digest_store.clear_due(conn, paper_id, topic)
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


async def settle_topic(conn: psycopg.AsyncConnection, slug: str) -> int:
    """Settle every paper the topic has a grade, verdict or word for, in id order, without
    announcing: after its kind or content changed, so a topic that stopped being Watch takes
    its grades with it and takes bound to the old rubric are owed again. Call it in the
    transaction that saved the topic, after `topics.kind_for_update`."""
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
        await settle(conn, row["paper_id"], announce=False)
    return len(rows)


async def sync(conn: psycopg.AsyncConnection) -> dict[str, int]:
    """Settle every paper that has a verdict, a word or a library row. For an adopted or
    seeded database, and after anything that wrote verdicts or labels around `settle`.

    Nothing it moves is announced in an issue: these papers were brought in, not found. The
    writer still owes each shown pair a take.
    """
    counts = {"papers": 0, "grades_changed": 0, "saved": 0, "unsaved": 0}
    for paper_id in await word_store.touched(conn):
        settled = await settle(conn, paper_id, announce=False)
        counts["papers"] += 1
        counts["grades_changed"] += len(settled.grades)
        if settled.saved != settled.was_saved:
            counts["saved" if settled.saved else "unsaved"] += 1
    return counts
