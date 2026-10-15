"""Classification (docs/specs/v1/pipeline.md, section 2): one decision request per paper, built
from the tree, read back as class probabilities and a primary class, and turned into direct
memberships with their sources. `classify` asks and writes nothing; `record` stores what it found.
The comparison of classifiers (`litwatch eval classify`) uses the same request."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import psycopg

from litwatch import events
from litwatch.core import Paper
from litwatch.core.classification import (
    CHOSEN,
    Classified,
    Membership,
    Source,
    Thresholds,
    keyword,
    memberships,
    standing,
)
from litwatch.core.taxonomy import Taxonomy
from litwatch.feedback import word
from litwatch.llm import CallError, Gateway, Usage
from litwatch.llm.decisions import Decision
from litwatch.llm.types import Choice, DecisionRequest, Predicate, Question
from litwatch.store import classifications as store
from litwatch.store import failures
from litwatch.store import papers as papers_store

ROLE = "classify"
# Not a possible class slug (slugs have no underscore), so it never names a yes/no question.
PRIMARY = "primary_class"
OWNER = "owner"  # the model of a classification by hand


def questions(tree: Taxonomy) -> tuple[Question, ...]:
    """A yes/no question per class and one choice of the primary class, offered by name:
    decision models choose by an option's name more than by its definition."""
    per_class = tuple(
        Predicate(c.slug, f"Does this arXiv paper belong to {c.name}: {c.description}")
        for c in tree.classes
    )
    primary = Choice(
        PRIMARY,
        "Which one class does this arXiv paper belong to most? Judge by its main contribution, "
        "not by every subject it mentions. Choose the most specific class that fits; choose a "
        "parent only when none of its children fits.",
        tuple((c.name, c.description) for c in tree.classes),
    )
    return (*per_class, primary)


def request(tree: Taxonomy, title: str, abstract: str) -> DecisionRequest:
    return DecisionRequest(f"Title: {title}\n\nAbstract: {abstract}", questions(tree))


def classified(tree: Taxonomy, decision: Decision) -> Classified:
    """The decision in class slugs. The gateway has checked that the choice is an option, so
    it names a class of this tree."""
    by_name = {c.name: c.slug for c in tree.classes}
    return Classified(
        probabilities=decision.probabilities,
        primary=by_name[decision.choices[PRIMARY].option],
    )


@dataclass
class Classification:
    """One paper's classification under one tree, or why there is none."""

    paper: Paper
    taxonomy_revision: str  # `Taxonomy.classes_version` of the tree it was asked under
    versions: dict[str, str]  # class slug -> its version in that tree
    classified: Classified | None = None
    memberships: list[Membership] = field(default_factory=list)
    response: dict[str, Any] | None = None  # the decision as the model gave it
    model: str | None = None
    usage: Usage | None = None
    error: str | None = None
    # The model answered and the answer could not be used: a failed run toward awaiting
    # classification. Any other error (a timeout, a provider down) is retried without counting.
    unusable: bool = False


async def _one(
    paper: Paper, tree: Taxonomy, gateway: Gateway, thresholds: Thresholds
) -> Classification:
    found = Classification(paper, tree.classes_version, {c.slug: c.version for c in tree.classes})
    try:
        answer = await gateway.decide(
            ROLE, request(tree, paper.title, paper.abstract), paper_id=paper.id
        )
    except CallError as exc:
        # Every question answered, every probability in [0, 1], the choice a class of the tree:
        # anything less is a failed call (pipeline.md, section 2).
        found.error, found.usage, found.unusable = str(exc), exc.usage, exc.unusable
        return found
    found.classified = classified(tree, answer.value)
    found.memberships = memberships(tree, found.classified, paper.title, paper.abstract, thresholds)
    found.response = answer.value.model_dump(mode="json")
    found.model, found.usage = answer.model, answer.usage
    return found


async def classify(
    papers: list[Paper],
    tree: Taxonomy,
    gateway: Gateway,
    thresholds: Thresholds = CHOSEN,
    concurrency: int = 16,
) -> AsyncIterator[Classification]:
    """Ask the `classify` role about each paper and yield results as they arrive. Writes
    nothing. A paper whose call fails is yielded with its error and stops nothing else. Close
    it (`contextlib.aclosing`) when leaving early, so no call goes on behind the caller."""
    limit = asyncio.Semaphore(concurrency)

    async def bounded(paper: Paper) -> Classification:
        async with limit:
            return await _one(paper, tree, gateway, thresholds)

    tasks = [asyncio.create_task(bounded(p)) for p in papers]
    try:
        for done in asyncio.as_completed(tasks):
            yield await done
    finally:
        # Left early (the caller failed or stopped): no call may outlive the run that pays
        # for it, so every worker is cancelled and awaited before the generator closes.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _announce(
    conn: psycopg.AsyncConnection, paper: Paper, primary: str, by_owner: bool
) -> None:
    """`paper.classified`, with the classes the paper is a direct member of now that it is
    recorded, the owner's labels applied."""
    classes = sorted({m.slug for m in standing(await store.memberships(conn, paper.id))})
    await events.emit(
        conn,
        events.PAPER_CLASSIFIED,
        paper.id,
        revision=paper.revision,
        primary=primary,
        classes=classes,
        by_owner=by_owner,
    )


async def record(conn: psycopg.AsyncConnection, c: Classification) -> bool:
    """Store one paper's classification, or record its failure. Returns False when there was
    nothing to record: the paper's text changed meanwhile, or it was dismissed.

    The classifier's memberships replace the paper's earlier ones; the owner's class labels
    stay and outrank them (`core.classification.standing`). The paper is settled: classified,
    it is in the collection and has a rarity, from its Weight alone until it is graded.
    """
    async with conn.transaction():
        await word.lock(conn, c.paper.id)  # before any write, as every write that settles
        if not await papers_store.still_current(conn, c.paper):
            return False
        if c.error is not None or c.classified is None or c.model is None:
            error = c.error or "no answer"
            await failures.failed(conn, c.paper, "classification", error, counts=c.unusable)
            return True
        primary = c.classified.primary
        await store.add(conn, c.paper, c.taxonomy_revision, primary, c.response, c.model)
        await store.replace(conn, c.paper, c.memberships, c.versions)
        await failures.succeeded(conn, c.paper.id, "classification")
        await _announce(conn, c.paper, primary, by_owner=False)
        await word.settle(conn, c.paper.id)
    return True


async def by_hand(
    conn: psycopg.AsyncConnection,
    paper: Paper,
    tree: Taxonomy,
    classes: list[str],
    primary: str | None = None,
) -> bool:
    """The owner classifies a paper: its classes, each an owner membership, and its primary
    class (the first named, unless given). The keyword check runs as usual. Raises ValueError
    for a class not in the tree. Returns False when the paper's text changed meanwhile, or it
    was dismissed. The caller settles the paper once its transaction ends (`word.settle`)."""
    if not classes:
        raise ValueError("name at least one class")
    if unknown := sorted(set(classes) - {c.slug for c in tree.classes}):
        raise ValueError(f"not classes of the tree: {', '.join(unknown)}")
    primary = primary or classes[0]
    if primary not in classes:
        raise ValueError("the primary class must be one of the classes named")
    versions = {c.slug: c.version for c in tree.classes}
    hits = keyword(tree, paper.title, paper.abstract)
    async with conn.transaction():
        if not await papers_store.still_current(conn, paper):
            return False
        await store.add(conn, paper, tree.classes_version, primary, None, OWNER)
        for slug in dict.fromkeys(classes):
            await store.put_owner(conn, paper, slug, True, versions[slug])
        found = [Membership(slug=f, source=Source.KEYWORD) for f in sorted(hits)]
        await store.replace(conn, paper, found, versions)
        await failures.succeeded(conn, paper.id, "classification")
        await _announce(conn, paper, primary, by_owner=True)
    return True
