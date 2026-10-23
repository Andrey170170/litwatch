"""The owner's word and what follows from it.

Every change that can move a paper (a verdict, a Weight, a label, a thumb, the Save toggle, an
edit of the followed topics, a new arXiv version) ends in `settle`, which recomputes the paper's
effective Fit and grade per followed topic, its rarity and whether it is in the library, stores
them, and emits `grade.changed`, `paper.saved` and `paper.unsaved` in the same transaction.
Whoever settles names the `Cause`, which `grade.changed` carries. It
also queues the paper's full-text passes when it qualifies for them (its Brief or Weight
re-check, its Deep card), so the owner's word can queue one too, and drops one not started that
it no longer qualifies for.

A change to a paper goes through `change`, which opens the transaction, takes the locks in
their order, runs the action's checks and writes, and settles each paper once before commit;
the helpers below (`label`, `react`, `toggle`, …) are changes, alone or inside a caller's.
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace

import psycopg

from litwatch import events
from litwatch.core import (
    Cause,
    Depth,
    Effective,
    Fit,
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
    page_standing,
)
from litwatch.core.fulltext import (
    TEXT_PRIORITY,
    PassKind,
    PassState,
    deep_to_drop,
    deep_to_queue,
    deep_to_relabel,
    deep_wanted,
    to_drop,
    to_queue,
    to_relabel,
    wanted,
)
from litwatch.store import analyses as analyses_store
from litwatch.store import feedback as feedback_store
from litwatch.store import fulltext as fulltext_store
from litwatch.store import jobs as jobs_store
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


async def lock_many(conn: psycopg.AsyncConnection, paper_ids: list[str]) -> None:
    """Hold the papers' settle locks to the end of the transaction, taken in id order so that
    two transactions locking papers in common never wait on each other. Every write that ends
    in a settle takes them first: through `change`, or intake's batch (`intake.batch.store`).

    The taxonomy lock is taken shared first: a taxonomy edit takes it exclusively before any
    paper, so locks are always taken taxonomy first, and a settle never reads a depth that an
    edit in flight is about to change.
    """
    await taxonomy_store.lock_shared(conn)
    await word_store.lock_settles(conn, sorted(set(paper_ids)))


@dataclass
class Change:
    """A change to some papers (`change`): the papers it locked, in id order, and what settling
    them did, filled when the outermost change exits."""

    paper_ids: tuple[str, ...]
    settled: dict[str, Settled] = field(default_factory=dict)


# The change open on each connection, which a nested `change` joins.
_open: weakref.WeakKeyDictionary[psycopg.AsyncConnection, Change] = weakref.WeakKeyDictionary()


@asynccontextmanager
async def change(
    conn: psycopg.AsyncConnection, *paper_ids: str, cause: Cause
) -> AsyncIterator[Change]:
    """Change the papers in one transaction: their locks first (`lock_many`), then the block's
    checks and writes, then a settle of each paper, in id order, before commit, for `cause`. The
    block may return early with nothing to record: the papers are settled all the same, which
    changes nothing then. An exception rolls the change back, unsettled.

    Inside another change on the connection, it joins that one, in a savepoint: the outermost
    change settles once at its exit, for its own cause, and only then is `settled` filled. A
    nested change names only papers the outer one locked; a lock taken late would break the
    order, so it raises.
    """
    outer = _open.get(conn)
    if outer is not None:
        if late := sorted(set(paper_ids) - set(outer.paper_ids)):
            raise RuntimeError(f"a nested change on papers the outer one did not lock: {late}")
        async with conn.transaction():
            yield outer
        return
    current = Change(tuple(sorted(set(paper_ids))))
    async with conn.transaction():
        await lock_many(conn, list(current.paper_ids))
        _open[conn] = current
        try:
            yield current
            for paper_id in current.paper_ids:
                current.settled[paper_id] = await settle(conn, paper_id, cause=cause)
        finally:
            del _open[conn]


def _value(e: Effective | None) -> dict[str, str | None]:
    return {"grade": e.grade.value if e else None, "fit": e.fit.value if e else None}


async def settle(
    conn: psycopg.AsyncConnection, paper_id: str, *, cause: Cause, claimed: int | None = None
) -> Settled:
    """Recompute and store the paper's effective Fits and grades, its rarity and its library
    state, with their events, its text pass, and its Process job's. `cause` is what
    changed them, for `grade.changed`; a `verdict` that left the Fit where it was is reported as
    `weight`.
    `claimed` is a pass the caller holds and has not worked yet: it is judged as one not
    started, so it may be dropped or relabelled.

    Settles of one paper are serialized and read only after the lock is held, so the last to
    run sees every committed change and leaves the stored state right.
    """

    async with conn.transaction():
        await lock_many(conn, [paper_id])
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
                moved = cause
                if cause == Cause.VERDICT and b["fit"] == a["fit"]:
                    moved = Cause.WEIGHT
                await events.emit(
                    conn,
                    events.GRADE_CHANGED,
                    paper_id,
                    topic=topic,
                    before=b["grade"],
                    after=a["grade"],
                    fit_before=b["fit"],
                    fit_after=a["fit"],
                    cause=moved.value,
                )

        # Rarity, for a paper in the collection: the followed topics' page levels in the owner's
        # order, lowered by depth, never below the Weight's floor. The Weight is kept beside it.
        if await word_store.in_collection(conn, paper_id):
            levels = [(s, t.depth, after[s].rarity) for s, t in topics.items() if s in after]
            rarity = (*mixed_rarity(levels, weight), weight)
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
        await _text_pass(conn, paper_id, topics, after, claimed)
        await _deep_pass(conn, paper_id, topics, after, hand == Hand.UNSAVE, claimed)
        # A paper the owner had processed: its passes go as their requests do, whoever ran its
        # steps (the job, or a nightly run before it), its failed job put back first.
        await jobs_store.settle_process(conn, paper_id, owner=cause == Cause.OWNER)
        if await jobs_store.processing(conn, paper_id):
            await fulltext_store.owners_first(conn, [paper_id])
    return result


async def _text_pass(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    topics: dict,
    after: dict[str, Effective],
    claimed: int | None,
) -> None:
    """Queue the paper's Brief or Weight re-check when it qualifies on its effective Fits and
    grades in active topics, an Area's including its descendants' (pipeline.md, sections 4
    and 6), or again for a newer arXiv version
    (section 8); relabel one not started that it now wants for another reason, and drop one it
    no longer qualifies for."""
    found = await fulltext_store.facts(conn, paper_id)
    if found is None or found.dismissed:
        return
    last, ours = found.last, found.last is not None and found.last.id == claimed
    if ours and last.state == PassState.RUNNING:
        last = replace(last, state=PassState.READY)
    await fulltext_store.drop_stale(conn, paper_id, found.revision, claimed)
    active = [t for t in topics.values() if t.status == Status.ACTIVE]
    tree = await taxonomy_store.load(conn)

    def must_read_in(area: str) -> bool:
        # An Area's pick candidates include its subtree's papers (section 4): its own grade,
        # else the highest among its followed descendants'.
        below = [(t.slug, after[t.slug]) for t in tree.under(area) if t.slug in after]
        standing = page_standing(after.get(area), [b for b in below if b[0] != area], None)
        return standing.grade == Grade.MUST_READ

    want = wanted(
        focus=any(
            t.depth == Depth.FOCUS
            and t.slug in after
            and after[t.slug].fit in (Fit.CENTRAL, Fit.RELATED)
            for t in active
        ),
        area=tree is not None
        and any(t.depth == Depth.AREA and must_read_in(t.slug) for t in active),
        has_text_result=found.has_text_result,
    )
    if to_drop(want, last):
        await fulltext_store.drop(conn, last.id, claimed=ours)
    elif want is not None and to_relabel(want, last):
        await fulltext_store.relabel(conn, last.id, *want, claimed=ours)
    elif want is not None and to_queue(want, last, found.version):
        kind, reason = want
        await fulltext_store.queue(conn, paper_id, found.revision, kind, reason, TEXT_PRIORITY)


async def _deep_pass(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    topics: dict,
    after: dict[str, Effective],
    unsaved: bool,
    claimed: int | None,
) -> None:
    """Queue the paper's automatic Deep card when an active Focus topic grades it must read, or
    a published digest of an active Area topic picked it, it is not unsaved and it has no card;
    relabel one not started that it wants for the other reason now, and drop one it no longer
    wants (an Unsave, a paused topic). The owner's request is left alone."""
    found = await fulltext_store.deep_facts(conn, paper_id)
    if found is None or found.dismissed:
        return
    open_pass = found.open_pass
    ours = open_pass is not None and open_pass.id == claimed
    if ours and open_pass.state == PassState.RUNNING:
        open_pass = replace(open_pass, state=PassState.READY)
    active = [t for t in topics.values() if t.status == Status.ACTIVE]
    focus_must_read = any(
        t.depth == Depth.FOCUS and t.slug in after and after[t.slug].grade == Grade.MUST_READ
        for t in active
    )
    picked = any(t.depth == Depth.AREA and t.slug in found.picked_by for t in active)
    want = deep_wanted(focus_must_read, picked, unsaved, found.has_card)
    if deep_to_drop(want, open_pass):
        await fulltext_store.drop(conn, open_pass.id, claimed=ours)
    elif want is not None and deep_to_relabel(want, open_pass):
        await fulltext_store.relabel(conn, open_pass.id, PassKind.DEEP, want, claimed=ours)
    elif want is not None and deep_to_queue(want, open_pass):
        await fulltext_store.queue_deep(conn, paper_id, found.revision, want)


# Each helper below is a change of one paper (`change`): alone it settles the paper before it
# returns; inside a caller's change, that change settles it at its exit.


async def label(conn: psycopg.AsyncConnection, given: Label) -> tuple[Label, Change]:
    async with change(conn, given.paper_id, cause=Cause.OWNER) as done:
        stored = await feedback_store.set_label(conn, given)
    return stored, done


async def unlabel(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> Change:
    async with change(conn, paper_id, cause=Cause.OWNER) as done:
        await feedback_store.delete_label(conn, paper_id, topic)
    return done


async def react(conn: psycopg.AsyncConnection, given: Reaction) -> tuple[Reaction, Change]:
    async with change(conn, given.paper_id, cause=Cause.OWNER) as done:
        stored = await word_store.set_reaction(conn, given)
    return stored, done


async def unreact(conn: psycopg.AsyncConnection, paper_id: str, topic: str | None) -> Change:
    """Clear a thumb. It takes back its own word and nothing else."""
    async with change(conn, paper_id, cause=Cause.OWNER) as done:
        await word_store.clear_reaction(conn, paper_id, topic)
    return done


async def toggle(conn: psycopg.AsyncConnection, paper_id: str, hand: Hand) -> Change:
    """Save or unsave by hand. The newest use outranks every other reason, until the next."""
    async with change(conn, paper_id, cause=Cause.OWNER) as done:
        await word_store.set_hand(conn, paper_id, hand)
    return done


async def save_taxonomy(conn: psycopg.AsyncConnection, tree: Taxonomy) -> taxonomy_store.Saved:
    """Save the taxonomy and, in the same transaction, settle the papers of every topic it
    changed, or under a topic or class it changed: a topic no longer followed takes its grades
    with it, a new depth moves its papers' rarity, one no longer at Focus its papers' place in
    the library, and an Area's standing follows the topics under it."""
    async with conn.transaction():
        saved = await taxonomy_store.save(conn, tree)
        # And those of the topics under each, and under a class revised (moved, say): an Area's
        # standing comes from the topics under it too.
        roots = [*saved.changed_topics, *saved.revised_classes]
        below = {t.slug for slug in roots for t in tree.under(slug)}
        for slug in sorted(set(saved.changed_topics) | below):
            await settle_topic(conn, slug)
        if saved.reordered:
            await settle_ties(conn)
    return saved


async def reorder(conn: psycopg.AsyncConnection, slugs: list[str]) -> None:
    """Store the owner's order of the topics (`taxonomy.set_order`; ValueError unless it names
    every topic once) and, in the same transaction, settle the papers whose best topic it
    decides."""
    async with conn.transaction():
        await taxonomy_store.set_order(conn, slugs)
        await settle_ties(conn)


async def settle_ties(conn: psycopg.AsyncConnection) -> int:
    """Settle the papers whose best topic the owner's order breaks a tie for (`word.tied`):
    the only papers a new order changes. Call it after `taxonomy.lock`."""
    papers = await word_store.tied(conn)
    for paper_id in papers:
        await settle(conn, paper_id, cause=Cause.TAXONOMY)
    return len(papers)


async def settle_all(conn: psycopg.AsyncConnection, paper_ids: list[str], *, cause: Cause) -> None:
    """Settle each paper in turn (`settle`) for `cause`, but one a change open on the
    connection holds: that change settles it at its exit, once, for its own cause (a paper an
    owner's action fetched and stored inside its change, say)."""
    outer = _open.get(conn)
    held = set(outer.paper_ids) if outer else set()
    for paper_id in paper_ids:
        if paper_id not in held:
            await settle(conn, paper_id, cause=cause)


async def settle_topic(conn: psycopg.AsyncConnection, slug: str) -> int:
    """Settle every paper the topic has a grade, verdict, word or grading failure for, in id
    order (`word.topic_papers`). Call it in the transaction that changed the topic, after
    `taxonomy.lock`."""
    papers = await word_store.topic_papers(conn, slug)
    for paper_id in papers:
        await settle(conn, paper_id, cause=Cause.TAXONOMY)
    return len(papers)


async def sync(conn: psycopg.AsyncConnection) -> dict[str, int]:
    """Settle every paper in the collection or with a verdict, a word or a library row: after
    anything that wrote verdicts, Weights or labels around `settle`. What it finds changed is
    reported as the verdict's (a Weight's, when the Fit did not move)."""
    counts = {"papers": 0, "grades_changed": 0, "saved": 0, "unsaved": 0}
    touched = await word_store.touched(conn)
    for paper_id in touched:
        settled = await settle(conn, paper_id, cause=Cause.VERDICT)
        counts["papers"] += 1
        if counts["papers"] % SYNC_PROGRESS == 0:
            log.info("settled %s of %s papers", counts["papers"], len(touched))
        counts["grades_changed"] += len(settled.grades)
        if settled.saved != settled.was_saved:
            counts["saved" if settled.saved else "unsaved"] += 1
    return counts
