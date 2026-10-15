"""Grading (docs/specs/v1/pipeline.md, section 4): the Fit of a paper for each topic that grades
it, checked as the topic's depth says. Focus: `judge` and `check` at once, the more inclusive
Fit standing. Area: `judge`, then the `area_check` decision model on whether its Fit holds,
escalating to `check` on doubt, the less inclusive Fit standing. Field: `field_grade` alone.
`grade` asks and writes nothing; `record` stores what it found and settles the paper."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import psycopg
from pydantic import BaseModel, Field

from litwatch import events
from litwatch.config import Grading
from litwatch.core import Depth, Fit, Opinion, Paper, Status, Taxonomy, Topic, Verdict
from litwatch.core.grading import FIT_DEFINITIONS, grading_topics
from litwatch.core.grading import grade as derived
from litwatch.feedback import word
from litwatch.llm import CallError, Gateway, Request
from litwatch.llm.types import DecisionRequest, Predicate
from litwatch.store import analyses as analyses_store
from litwatch.store import failures
from litwatch.store import papers as papers_store
from litwatch.store import verdicts as store

HOLDS = "holds"  # the `area_check` question
_LEVELS = "\n".join(f"- {f.value}: {meaning}" for f, meaning in FIT_DEFINITIONS.items())

SYSTEM = f"""\
You judge how close one arXiv paper is to one research topic, from its title and abstract, for \
litwatch, which reads every new paper in a field. Answer with the structured output only.

The topic comes with the paper: its name and its subject, which says what is in the topic and, \
where a neighbouring topic is close, what is out. A Focus topic also has a rubric that spells \
out what central, related and adjacent mean for it; where the rubric places a kind of paper at \
a level, the rubric wins over the general definitions below, and anything below adjacent is \
none.

fit: the paper's Fit to the topic, one of:
{_LEVELS}

reason: one sentence why, about this paper and this topic. Write about the paper, never to a \
reader: no "you", no audience, no advice on whether to read it."""

AREA_CHECK = (
    "A grader judged how close this arXiv paper is to the topic given with it and proposed the "
    "Fit shown, with its reason. Does the proposed Fit hold for this paper and this topic? The "
    f"Fit levels:\n{_LEVELS}"
)


class Output(BaseModel):
    """A grader's answer. The reason comes before the Fit it argues for."""

    reason: str = Field(description="One sentence")
    fit: Fit


def content(paper: Paper, topic: Topic, name: str) -> str:
    """The topic and the paper, as every grader reads them: subject for every topic, the rubric
    for a Focus topic."""
    rubric = f"Rubric: {topic.rubric}\n" if topic.depth == Depth.FOCUS and topic.rubric else ""
    return (
        f"Topic: {name} ({topic.depth.value})\nSubject: {topic.subject}\n{rubric}\n"
        f"Title: {paper.title}\n\nAbstract:\n{paper.abstract}"
    )


def request(paper: Paper, topic: Topic, name: str) -> Request:
    """The instructions repeat across papers and topics, so they are cached."""
    return Request(system=SYSTEM, user=content(paper, topic, name), output=Output)


def check_request(paper: Paper, topic: Topic, name: str, proposed: Opinion) -> DecisionRequest:
    proposal = f"\n\nProposed Fit: {proposed.fit}\nWhy: {proposed.reason}"
    return DecisionRequest(content(paper, topic, name) + proposal, (Predicate(HOLDS, AREA_CHECK),))


@dataclass(frozen=True)
class Due:
    """A grading to do: the paper's current text for one topic, with its class's name."""

    paper: Paper
    topic: Topic
    name: str


@dataclass
class Graded:
    """One paper's grading for one topic, or why there is none."""

    due: Due
    verdict: Verdict | None = None
    error: str | None = None
    # Some grader answered and the answer could not be used: a failed run that counts.
    unusable: bool = False


async def _fit(gateway: Gateway, role: str, d: Due) -> Opinion:
    answer = await gateway.ask(role, request(d.paper, d.topic, d.name), paper_id=d.paper.id)
    return Opinion(model=answer.model, fit=answer.value.fit, reason=answer.value.reason)


async def _holds(gateway: Gateway, d: Due, proposed: Opinion) -> Opinion:
    asked = check_request(d.paper, d.topic, d.name, proposed)
    answer = await gateway.decide("area_check", asked, paper_id=d.paper.id)
    p = answer.value.probabilities[HOLDS]
    return Opinion(model=answer.model, holds=p >= 0.5, probability=p)


def _verdict(d: Due, opinions: dict[str, Opinion], standing: Opinion, **marks: bool) -> Verdict:
    return Verdict(
        paper_id=d.paper.id,
        topic=d.topic.slug,
        topic_version=d.topic.version,
        paper_revision=d.paper.revision,
        fit=standing.fit,
        reason=standing.reason,
        opinions=opinions,
        **marks,
    )


async def _graded(d: Due, gateway: Gateway, settings: Grading) -> Verdict:
    """The verdict, or the first `CallError` of a grader it needed."""
    match d.topic.depth:
        case Depth.FOCUS:
            # Concurrently, and both answers awaited even when one fails.
            answers = await asyncio.gather(
                _fit(gateway, "judge", d), _fit(gateway, "check", d), return_exceptions=True
            )
            failed = [a for a in answers if isinstance(a, BaseException)]
            if bug := next((e for e in failed if not isinstance(e, CallError)), None):
                raise bug
            if failed:  # a failed run that counts when either grader answered unusably
                raise next((e for e in failed if e.unusable), failed[0])
            judge, check = answers
            standing = min(answers, key=lambda o: o.fit.rank)  # the more inclusive
            opinions = {"judge": judge, "check": check}
            return _verdict(d, opinions, standing, split=judge.fit != check.fit)
        case Depth.AREA:
            judge = await _fit(gateway, "judge", d)
            holds = await _holds(gateway, d, judge)
            opinions = {"judge": judge, "area_check": holds}
            if holds.holds and holds.probability >= settings.area_check_min_confidence:
                return _verdict(d, opinions, judge)
            # A "no" or a doubtful "yes": the other lab grades too. An Area's risk is a false
            # pick, not a miss, so the less inclusive Fit stands.
            check = opinions["check"] = await _fit(gateway, "check", d)
            standing = max((judge, check), key=lambda o: o.fit.rank)
            return _verdict(d, opinions, standing, split=judge.fit != check.fit, escalated=True)
        case Depth.FIELD:
            alone = await _fit(gateway, "field_grade", d)
            return _verdict(d, {"field_grade": alone}, alone)
    raise AssertionError(d.topic.depth)


async def _one(d: Due, gateway: Gateway, settings: Grading) -> Graded:
    try:
        return Graded(d, await _graded(d, gateway, settings))
    except CallError as exc:
        # Truncated, invalid or refused: no verdict, and a failed run that counts. A grader that
        # did not answer (a timeout, a provider down) is retried without counting.
        return Graded(d, error=str(exc), unusable=exc.unusable)


async def grade(
    due: list[Due], gateway: Gateway, settings: Grading | None = None, concurrency: int = 8
) -> AsyncIterator[Graded]:
    """Grade each paper for its topic and yield results as they arrive. Writes nothing. A
    grading whose graders fail is yielded with its error and stops nothing else. Close it
    (`contextlib.aclosing`) when leaving early, so no call goes on behind the caller."""
    settings = settings or Grading()
    limit = asyncio.Semaphore(concurrency)

    async def bounded(d: Due) -> Graded:
        async with limit:
            return await _one(d, gateway, settings)

    tasks = [asyncio.create_task(bounded(d)) for d in due]
    try:
        for done in asyncio.as_completed(tasks):
            yield await done
    finally:
        # Left early (the caller failed or stopped): no call may outlive the run that pays
        # for it, so every worker is cancelled and awaited before the generator closes.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def due(
    conn: psycopg.AsyncConnection, tree: Taxonomy, runs: int, limit: int | None = None
) -> tuple[list[Due], list[tuple[str, int]]]:
    """The gradings due, for at most `limit` papers, and the (paper, revision) whose grading pass
    they make, to record with `store.passed` once they are recorded. A paper's first pass on its
    current text grades each active topic it leads to (`core.grading.grading_topics`); after
    that only a grading that failed is tried again, and a topic a miss was reported for is
    graded whatever classification said (feedback.md). A topic the paper leads to later waits
    for a re-run (pipeline.md, section 10), as does a topic edit, whatever the topic's version.
    New gradings first, oldest paper first; a grading that failed comes after them, and after
    `runs` failed runs it is not tried at all: it waits for the owner. A paused or draft topic
    grades nothing."""
    active = {t.slug: t for t in tree.topics if t.status == Status.ACTIVE}
    found: list[tuple[int, str, str]] = []  # (failed runs, paper, topic), oldest paper first
    fresh: dict[str, int] = {}  # paper → revision, for a first pass
    for row in await store.candidates(conn):
        if row["passed"]:
            should = set(row["failed"])
        else:
            should = grading_topics(tree, row["memberships"], row["held"])
            fresh[row["paper_id"]] = row["revision"]
        should |= set(row["missed"])
        for slug in sorted(should & active.keys() - set(row["graded"])):
            failed = row["failed"].get(slug, 0)
            if failed < runs:
                found.append((failed, row["paper_id"], slug))
    found.sort(key=lambda f: f[0] > 0)  # stable: new ones first, in paper order
    papers: list[str] = []
    for _, paper_id, _ in found:
        if paper_id not in papers:
            papers.append(paper_id)
    chosen = set(papers[:limit] if limit is not None else papers)
    by_id = {p.id: p for p in await papers_store.get_many(conn, sorted(chosen))}
    due = [
        Due(by_id[p], active[slug], tree.get(slug).name)
        for _, p, slug in found
        if p in chosen and p in by_id
    ]
    # A first pass with nothing to grade is done now; one left out by the limit is not.
    passes = [(p, rev) for p, rev in fresh.items() if p in chosen or p not in papers]
    return due, passes


async def record(conn: psycopg.AsyncConnection, g: Graded) -> bool:
    """Store one grading with `verdict.recorded` and settle the paper (its effective grade,
    rarity and library state follow); or record its failure. Returns False when there was
    nothing to record: the paper's text changed meanwhile, or it was dismissed."""
    paper, slug = g.due.paper, g.due.topic.slug
    async with conn.transaction():
        await word.lock(conn, paper.id)
        if not await papers_store.still_current(conn, paper):
            return False
        if g.verdict is None:
            error = g.error or "no answer"
            await failures.failed(conn, paper, "grading", error, counts=g.unusable, topic=slug)
            return True
        stored = await store.add(conn, g.verdict)
        await failures.succeeded(conn, paper.id, "grading", slug)
        weight = await analyses_store.current_weight(conn, paper.id)
        await events.emit(
            conn,
            events.VERDICT_RECORDED,
            paper.id,
            topic=slug,
            revision=paper.revision,
            verdict_id=stored.id,
            fit=stored.fit.value,
            weight=weight.value if weight else None,
            grade=derived(stored.fit, weight).value,
            split=stored.split,
            escalated=stored.escalated,
        )
        await word.settle(conn, paper.id)
    return True
