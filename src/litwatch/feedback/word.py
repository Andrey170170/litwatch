"""The owner's word and what follows from it.

Every change that can move a paper (a verdict, a Weight, a label, a thumb, the Save toggle, an
edit of the followed topics, a new arXiv version) ends in a settle (`settle_many`), which
recomputes each paper's effective Fit and grade per followed topic, its rarity and whether it is
in the library, stores them, and emits `grade.changed`, `paper.saved` and `paper.unsaved` in the
same unit. Whoever settles names the `Cause`, which `grade.changed` carries. It also queues the
paper's full-text passes when it qualifies for them (its Brief or Weight re-check, its Deep
card), so the owner's word can queue one too, and drops one not started that it no longer
qualifies for. What it takes from the configuration is its `Rules`: automatic Deep cards are
queued only while the `deep` integration is configured.

A change to papers goes through `change`, which opens a write unit of the Database gate holding
the locks, runs the action's checks and writes in it, and settles each paper once before commit.
What runs inside a change takes its unit; changes do not nest.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from litwatch import events
from litwatch.config import Config
from litwatch.core import (
    Cause,
    Depth,
    Effective,
    Fit,
    Grade,
    Hand,
    Label,
    Rarity,
    Reaction,
    Status,
    Taxonomy,
    Topic,
    Weight,
    effective,
    in_library,
    mixed_rarity,
    owners_word,
    page_standing,
)
from litwatch.core.fulltext import (
    AUTOMATIC_PRIORITY,
    BACKGROUND_PRIORITY,
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
from litwatch.gates.database import DatabaseGate, WriteUnit
from litwatch.integrations import enabled_using
from litwatch.store import analyses as analyses_store
from litwatch.store import feedback as feedback_store
from litwatch.store import fulltext as fulltext_store
from litwatch.store import jobs as jobs_store
from litwatch.store import outbox
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts as verdicts_store
from litwatch.store import word as word_store

log = logging.getLogger(__name__)
SYNC_PROGRESS = 2000  # `sync` logs its progress every this many papers
# The most papers settling reads at once, and the most a unit of `settle_all` or `sync` locks.
CHUNK = 500


@dataclass(frozen=True)
class Rules:
    """What settling takes from the configuration.

    `automatic_deep`: whether it queues automatic Deep cards. Off, it wants none, so it drops
    an automatic one not started; the owner's request is left alone either way."""

    automatic_deep: bool


def rules(config: Config) -> Rules:
    """The configuration's rules: automatic Deep cards only while an enabled integration loads
    `deep`. Whoever opens a change or settles reads them from the configuration it runs on."""
    return Rules(automatic_deep=enabled_using(config, "deep") is not None)


@dataclass
class Settled:
    """What a settle changed: effective grades as (before, after) per topic, and the library."""

    paper_id: str
    grades: dict[str, tuple[Grade | None, Grade | None]] = field(default_factory=dict)
    was_saved: bool = False
    saved: bool = False


@dataclass
class Change:
    """A change to some papers (`change`): its write unit, which the block's reads and writes
    run in; the papers it locked, in id order; and what settling them did, by paper. A block
    that settles a paper itself, after its last write that settling reads, records it here,
    and the change leaves it; the change fills in the rest when it exits."""

    unit: WriteUnit
    paper_ids: tuple[str, ...]
    settled: dict[str, Settled] = field(default_factory=dict)


@asynccontextmanager
async def change(
    db: DatabaseGate,
    *paper_ids: str,
    cause: Cause,
    rules: Rules,
    job: int | None = None,
    name: str = "change",
) -> AsyncIterator[Change]:
    """Change the papers in one write unit, `name`'s: the taxonomy lock, shared, and the papers'
    locks in id order as it opens, then the block's checks and writes, then a settle of each
    paper once, in id order, before commit, for `cause` (`settle_many`), but for those the
    block settled itself (`Change.settled`). The block may return
    early with nothing to record: the papers are settled all the same, which changes nothing
    then. An exception rolls the change back, unsettled.

    A change is a unit, so none opens inside another: what runs inside one takes its unit
    (`Change.unit`). A re-run's change names its `job`, whose passes the settle queues.
    """
    ids = tuple(sorted(set(paper_ids)))
    async with db.write(name, taxonomy="shared", papers=ids) as unit:
        current = Change(unit, ids)
        yield current
        rest = [i for i in ids if i not in current.settled]
        current.settled |= await settle_many(unit, rest, cause=cause, rules=rules, job=job)


def _value(e: Effective | None) -> dict[str, str | None]:
    return {"grade": e.grade.value if e else None, "fit": e.fit.value if e else None}


@dataclass
class _Read:
    """What settling a chunk of papers reads, each by paper, a paper with none left out."""

    labels: dict[str, list[Label]]
    reactions: dict[str, list[Reaction]]
    fits: dict[str, dict[str, Fit]]  # the current verdict's, per topic
    weights: dict[str, Weight]
    before: dict[str, dict[str, Effective]]  # the stored effective grades, per topic
    collected: set[str]  # the papers in the collection
    rarities: dict[str, tuple[Rarity, str | None, Weight | None]]
    library: dict[str, dict[str, Any]]
    text: dict[str, fulltext_store.Facts]
    deep: dict[str, fulltext_store.DeepFacts]
    with_process: set[str]  # the papers with a Process job


async def _read(unit: WriteUnit, paper_ids: list[str]) -> _Read:
    return _Read(
        labels=await feedback_store.labels_of(unit, paper_ids),
        reactions=await word_store.reactions_of(unit, paper_ids),
        fits=await verdicts_store.current_fits(unit, paper_ids),
        weights=await analyses_store.current_weights(unit, paper_ids),
        before=await word_store.effective_of(unit, paper_ids),
        collected=await word_store.collected(unit, paper_ids),
        rarities=await word_store.rarities_of(unit, paper_ids),
        library=await word_store.library_rows(unit, paper_ids),
        text=await fulltext_store.facts_of(unit, paper_ids),
        deep=await fulltext_store.deep_facts_of(unit, paper_ids),
        with_process=await jobs_store.with_process(unit, paper_ids),
    )


async def settle_many(
    unit: WriteUnit,
    paper_ids: Iterable[str],
    *,
    cause: Cause,
    rules: Rules,
    claimed: int | None = None,
    job: int | None = None,
    steps: bool = True,
) -> dict[str, Settled]:
    """Recompute and store each paper's effective Fits and grades, its rarity and its library
    state, with their events, its text pass, its Deep pass and its Process job's, each paper
    once, in id order. Returns what it changed, by paper. `cause` is what changed them, for
    `grade.changed`; a `verdict` that left the Fit where it was is reported as `weight`.
    `claimed` is a pass the caller holds and has not worked yet: it is judged as one not
    started, so it may be dropped or relabelled. For a re-run's `job`, a pass it queues is
    background work: a text pass the job's to work, under its ceiling, a Deep pass after the
    automatic ones (docs/plans/jobs.md). Without `steps`, the owner's change leaves the papers'
    steps as they were (a Deep request), so a running Process job of theirs is not asked again
    (`store.jobs.settle_process`).

    The unit holds the papers' locks, or the taxonomy lock exclusively (a taxonomy edit, which
    no other settle runs beside): settles of one paper are serialized, so the last to run sees
    every committed change and leaves the stored state right. The taxonomy is read once, when
    settling starts, so a unit that edited it settles against its edit; the papers' inputs
    (labels, thumbs, current verdicts, Weights, what is stored, their passes) are read a query
    each for `CHUNK` papers at a time. The rule itself is per paper.
    """
    ids = sorted(set(paper_ids))
    if not ids:
        return {}
    if unit.taxonomy != "exclusive" and (unlocked := sorted(set(ids) - unit.papers)):
        raise RuntimeError(f"unit {unit.name!r} settles papers it did not lock: {unlocked[:5]}")
    tree = await taxonomy_store.load(unit)
    # Followed topics in the owner's order; a draft topic is not used.
    topics = {t.slug: t for t in (tree.topics if tree else []) if t.status != Status.DRAFT}
    settled: dict[str, Settled] = {}
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        read = await _read(unit, chunk)
        for paper_id in chunk:
            settled[paper_id] = await _settle(
                unit, paper_id, topics, tree, read, cause, rules, claimed, job, steps
            )
    return settled


async def _settle(
    unit: WriteUnit,
    paper_id: str,
    topics: dict[str, Topic],
    tree: Taxonomy | None,
    read: _Read,
    cause: Cause,
    rules: Rules,
    claimed: int | None,
    job: int | None,
    steps: bool,
) -> Settled:
    """Settle one paper (`settle_many`) from what was read of it."""
    labels = {x.topic: x for x in read.labels.get(paper_id, [])}
    reactions = {r.topic: r for r in read.reactions.get(paper_id, [])}
    fits = read.fits.get(paper_id, {})
    weight = read.weights.get(paper_id)
    before = read.before.get(paper_id, {})

    result = Settled(paper_id)
    after: dict[str, Effective] = {}
    # A stored grade for a topic no longer followed is removed, with its event.
    involved = (set(topics) & (set(labels) | set(reactions) | set(fits))) | set(before)
    for topic in sorted(involved):
        new = None
        if t := topics.get(topic):
            fit = fits.get(topic)
            focus = t.depth == Depth.FOCUS
            word = owners_word(labels.get(topic), reactions.get(topic), fit, focus=focus)
            new = effective(word, fit, weight)
        if new is not None:
            after[topic] = new
        old = before.get(topic)
        if new == old:
            continue
        await word_store.set_effective(unit, paper_id, topic, new)
        if _value(new) != _value(old):
            result.grades[topic] = (old.grade if old else None, new.grade if new else None)
            b, a = _value(old), _value(new)
            moved = cause
            if cause == Cause.VERDICT and b["fit"] == a["fit"]:
                moved = Cause.WEIGHT
            await outbox.emit(
                unit,
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
    if paper_id in read.collected:
        levels = [(s, t.depth, after[s].rarity) for s, t in topics.items() if s in after]
        rarity = (*mixed_rarity(levels, weight), weight)
        if rarity != read.rarities.get(paper_id):
            await word_store.set_rarity(unit, paper_id, *rarity)

    row = read.library.get(paper_id)
    hand = Hand(row["hand"]) if row and row["hand"] else None
    result.was_saved = bool(row and row["saved"])
    focus_grades = [e.grade for s, e in after.items() if topics[s].depth == Depth.FOCUS]
    result.saved = in_library(hand, focus_grades)
    if result.saved != result.was_saved:
        await word_store.set_saved(unit, paper_id, result.saved)
        await outbox.emit(
            unit, events.PAPER_SAVED if result.saved else events.PAPER_UNSAVED, paper_id
        )
    await _text_pass(unit, paper_id, topics, tree, after, read.text.get(paper_id), claimed, job)
    await _deep_pass(
        unit,
        paper_id,
        topics,
        after,
        read.deep.get(paper_id),
        hand == Hand.UNSAVE,
        rules,
        claimed,
        job,
    )
    # A paper the owner had processed: its passes go as their requests do, whoever ran its
    # steps (the job, or a nightly run before it), its failed job put back first. A paper
    # without a Process job has none to settle.
    if paper_id in read.with_process:
        await jobs_store.settle_process(unit, paper_id, owner=cause == Cause.OWNER and steps)
        if await jobs_store.processing(unit, paper_id):
            await fulltext_store.owners_first(unit, [paper_id])
    return result


async def _text_pass(
    unit: WriteUnit,
    paper_id: str,
    topics: dict[str, Topic],
    tree: Taxonomy | None,
    after: dict[str, Effective],
    found: fulltext_store.Facts | None,
    claimed: int | None,
    job: int | None = None,
) -> None:
    """Queue the paper's Brief or Weight re-check when it qualifies on its effective Fits and
    grades in active topics, an Area's including its descendants' (pipeline.md, sections 4
    and 6), or again for a newer arXiv version
    (section 8); relabel one not started that it now wants for another reason, and drop one it
    no longer qualifies for."""
    if found is None or found.dropped:
        return
    last, ours = found.last, found.last is not None and found.last.id == claimed
    if ours and last.state == PassState.RUNNING:
        last = replace(last, state=PassState.READY)
    # Only a pass not done can be stale, and the caller's own, whatever its state.
    if found.stale or claimed is not None:
        await fulltext_store.drop_stale(unit, paper_id, found.revision, claimed)
    active = [t for t in topics.values() if t.status == Status.ACTIVE]

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
        await fulltext_store.drop(unit, last.id, claimed=ours)
    elif want is not None and to_relabel(want, last):
        await fulltext_store.relabel(unit, last.id, *want, claimed=ours)
    elif want is not None and to_queue(want, last, found.version):
        kind, reason = want
        priority = TEXT_PRIORITY if job is None else BACKGROUND_PRIORITY
        await fulltext_store.queue(unit, paper_id, found.revision, kind, reason, priority, job)


async def _deep_pass(
    unit: WriteUnit,
    paper_id: str,
    topics: dict[str, Topic],
    after: dict[str, Effective],
    found: fulltext_store.DeepFacts | None,
    unsaved: bool,
    rules: Rules,
    claimed: int | None,
    job: int | None = None,
) -> None:
    """Queue the paper's automatic Deep card when an active Focus topic grades it must read, or
    a published digest of an active Area topic picked it, it is not unsaved and it has no card;
    relabel one not started that it wants for the other reason now, and drop one it no longer
    wants (an Unsave, a paused topic, `rules.automatic_deep` off). The owner's request is left
    alone."""
    if found is None or found.dropped:
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
    want = None
    if rules.automatic_deep:
        want = deep_wanted(focus_must_read, picked, unsaved, found.has_card)
    if deep_to_drop(want, open_pass):
        await fulltext_store.drop(unit, open_pass.id, claimed=ours)
    elif want is not None and deep_to_relabel(want, open_pass):
        await fulltext_store.relabel(unit, open_pass.id, PassKind.DEEP, want, claimed=ours)
    elif want is not None and deep_to_queue(want, open_pass):
        priority = AUTOMATIC_PRIORITY if job is None else BACKGROUND_PRIORITY
        await fulltext_store.queue_deep(unit, paper_id, found.revision, want, priority)


# Each helper below writes the owner's word in the unit of an open change of the paper
# (`change`), which settles the paper at its exit.


async def label(unit: WriteUnit, given: Label) -> Label:
    return await feedback_store.set_label(unit, given)


async def unlabel(unit: WriteUnit, paper_id: str, topic: str) -> None:
    await feedback_store.delete_label(unit, paper_id, topic)


async def react(unit: WriteUnit, given: Reaction) -> Reaction:
    return await word_store.set_reaction(unit, given)


async def unreact(unit: WriteUnit, paper_id: str, topic: str | None) -> None:
    """Clear a thumb. It takes back its own word and nothing else."""
    await word_store.clear_reaction(unit, paper_id, topic)


async def toggle(unit: WriteUnit, paper_id: str, hand: Hand) -> None:
    """Save or unsave by hand. The newest use outranks every other reason, until the next."""
    await word_store.set_hand(unit, paper_id, hand)


# A taxonomy edit runs in a write unit holding the taxonomy lock exclusively, which its caller
# opens (`db.write(..., taxonomy="exclusive")`): no settle runs beside it, so it settles the
# papers it reaches without their locks.


async def save_taxonomy(unit: WriteUnit, tree: Taxonomy, *, rules: Rules) -> taxonomy_store.Saved:
    """Save the taxonomy and, in the same unit, settle the papers of every topic it changed, or
    under a topic or class it changed, each once: a topic no longer followed takes its grades
    with it, a new depth moves its papers' rarity, one no longer at Focus its papers' place in
    the library, and an Area's standing follows the topics under it. A new order of the topics
    settles the papers whose best topic it decides too."""
    saved = await taxonomy_store.save(unit, tree)
    # And those of the topics under each, and under a class revised (moved, say): an Area's
    # standing comes from the topics under it too.
    roots = [*saved.changed_topics, *saved.revised_classes]
    below = {t.slug for slug in roots for t in tree.under(slug)}
    papers = set(await word_store.topic_papers(unit, sorted(set(saved.changed_topics) | below)))
    if saved.reordered:
        # A tie the edit made or broke is on a paper of a changed topic, settled above anyway.
        papers |= set(await word_store.tied(unit))
    await settle_many(unit, papers, cause=Cause.TAXONOMY, rules=rules)
    return saved


async def reorder(unit: WriteUnit, slugs: list[str], *, rules: Rules) -> None:
    """Store the owner's order of the topics (`taxonomy.set_order`; ValueError unless it names
    every topic once) and, in the same unit, settle the papers whose best topic it decides."""
    await taxonomy_store.set_order(unit, slugs)
    await settle_ties(unit, rules=rules)


async def settle_ties(unit: WriteUnit, *, rules: Rules) -> int:
    """Settle the papers whose best topic the owner's order breaks a tie for (`word.tied`):
    the only papers a new order changes. Returns how many."""
    papers = await word_store.tied(unit)
    await settle_many(unit, papers, cause=Cause.TAXONOMY, rules=rules)
    return len(papers)


async def settle_topic(unit: WriteUnit, slug: str, *, rules: Rules) -> int:
    """Settle every paper the topic has a grade, verdict, word or grading failure for
    (`word.topic_papers`), in the unit that changed the topic. Returns how many."""
    papers = await word_store.topic_papers(unit, [slug])
    await settle_many(unit, papers, cause=Cause.TAXONOMY, rules=rules)
    return len(papers)


async def _settled(
    db: DatabaseGate, name: str, paper_ids: list[str], cause: Cause, rules: Rules
) -> AsyncIterator[dict[str, Settled]]:
    """Settle the papers (`settle_many`), `CHUNK` to a write unit of their own, giving what each
    unit settled once it has committed."""
    for start in range(0, len(paper_ids), CHUNK):
        chunk = paper_ids[start : start + CHUNK]
        async with db.write(name, taxonomy="shared", papers=chunk) as unit:
            done = await settle_many(unit, chunk, cause=cause, rules=rules)
        yield done


async def settle_all(
    db: DatabaseGate, paper_ids: Iterable[str], *, cause: Cause, rules: Rules
) -> None:
    """Settle the papers for `cause`, each once, `CHUNK` to a unit: for papers whatever they
    rest on may have changed around a change."""
    async for _ in _settled(db, "settle papers", sorted(set(paper_ids)), cause, rules):
        pass


async def sync(db: DatabaseGate, *, rules: Rules) -> dict[str, int]:
    """Settle every paper in the collection or with a verdict, a word or a library row: after
    anything that wrote verdicts, Weights or labels around a change. What it finds changed is
    reported as the verdict's (a Weight's, when the Fit did not move)."""
    counts = {"papers": 0, "grades_changed": 0, "saved": 0, "unsaved": 0}
    async with db.read("papers to sync") as unit:
        touched = await word_store.touched(unit)
    async for done in _settled(db, "sync", touched, Cause.VERDICT, rules):
        for settled in done.values():
            counts["papers"] += 1
            if counts["papers"] % SYNC_PROGRESS == 0:
                log.info("settled %s of %s papers", counts["papers"], len(touched))
            counts["grades_changed"] += len(settled.grades)
            if settled.saved != settled.was_saved:
                counts["saved" if settled.saved else "unsaved"] += 1
    return counts
