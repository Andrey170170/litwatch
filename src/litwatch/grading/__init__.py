"""Grading (docs/specs/v1/pipeline.md, section 4): the Fit of a paper for each topic that grades
it, checked as the topic's depth says. Focus: `judge` and `check` at once, the more inclusive
Fit standing, or the one side's Fit when the other's every model refused (`one_lab`). Area:
`judge`, then the `area_check` decision model on whether its Fit holds, escalating to `check` on
doubt, the less inclusive Fit standing. Field: `field_grade` alone. Each grader goes down its
role's list (`Gateway.ask`), from as far as the grading's failed runs say. `grade` asks and
writes nothing; `record` stores what it found and settles the paper."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

import psycopg
from pydantic import BaseModel, Field

from litwatch import events
from litwatch.concurrency import bounded
from litwatch.config import Grading
from litwatch.core import Cause, Depth, Fit, Opinion, Paper, Status, Taxonomy, Topic, Verdict
from litwatch.core.grading import FIT_DEFINITIONS, grading_topics
from litwatch.core.grading import grade as derived
from litwatch.feedback import word
from litwatch.llm import CallError, Gateway, Request
from litwatch.llm.types import DecisionRequest, Predicate
from litwatch.store import analyses as analyses_store
from litwatch.store import failures
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
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
    """A grading to do: the paper's current text for one topic, with its class's name, its
    failed runs so far (how far down each grader's list it starts, D4) and the Focus side whose
    every model refused on an earlier run, not asked again while the other side is."""

    paper: Paper
    topic: Topic
    name: str
    failed_runs: int = 0
    refused: frozenset[str] = frozenset()


@dataclass
class Graded:
    """One paper's grading for one topic, or why there is none."""

    due: Due
    verdict: Verdict | None = None
    error: str | None = None
    # Some grader answered and the answer could not be used: a failed run that counts.
    unusable: bool = False
    # The graders whose every model refused, this run or before; `stops` when that ends the
    # grading at once, ungraded (pipeline.md, section 4).
    refused: frozenset[str] = frozenset()
    stops: bool = False


async def _fit(gateway: Gateway, role: str, d: Due, refused: set[str]) -> Opinion:
    """The grader's Fit; a grader whose every model refused is added to `refused`."""
    try:
        answer = await gateway.ask(
            role, request(d.paper, d.topic, d.name), paper_id=d.paper.id, start=d.failed_runs
        )
    except CallError as exc:
        if exc.kind == "refused":
            refused.add(role)
        raise
    return Opinion(model=answer.model, fit=answer.value.fit, reason=answer.value.reason)


async def _holds(gateway: Gateway, d: Due, proposed: Opinion, refused: set[str]) -> Opinion:
    asked = check_request(d.paper, d.topic, d.name, proposed)
    try:
        answer = await gateway.decide("area_check", asked, paper_id=d.paper.id)
    except CallError as exc:
        if exc.kind == "refused":
            refused.add("area_check")
        raise
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


async def _graded(d: Due, gateway: Gateway, settings: Grading, refused: set[str]) -> Verdict:
    """The verdict, or the first `CallError` of a grader it needed; the graders whose every
    model refused are added to `refused`."""
    match d.topic.depth:
        case Depth.FOCUS:
            # Concurrently, and both answers awaited even when one fails. A side refused before
            # is not asked again (models.md, lab diversity).
            sides = [r for r in ("judge", "check") if r not in refused]
            answers = await asyncio.gather(
                *(_fit(gateway, r, d, refused) for r in sides), return_exceptions=True
            )
            failed = [a for a in answers if isinstance(a, BaseException)]
            if bug := next((e for e in failed if not isinstance(e, CallError)), None):
                raise bug
            if errors := [e for e in failed if e.kind != "refused"]:
                # A failed run that counts when either grader answered unusably; a side that
                # refused stays refused, and the other is asked again.
                raise next((e for e in errors if e.unusable), errors[0])
            if failed and len(refused) == 2:  # both sides refused throughout: ungraded
                raise failed[0]
            opinions = {r: a for r, a in zip(sides, answers, strict=True) if isinstance(a, Opinion)}
            if len(opinions) == 1:  # the other side refused throughout: one lab decides
                (alone,) = opinions.values()
                return _verdict(d, opinions, alone, one_lab=True)
            judge, check = opinions["judge"], opinions["check"]
            standing = min((judge, check), key=lambda o: o.fit.rank)  # the more inclusive
            return _verdict(d, opinions, standing, split=judge.fit != check.fit)
        case Depth.AREA:
            judge = await _fit(gateway, "judge", d, refused)
            holds = await _holds(gateway, d, judge, refused)
            opinions = {"judge": judge, "area_check": holds}
            if holds.holds and holds.probability >= settings.area_check_min_confidence:
                return _verdict(d, opinions, judge)
            # A "no" or a doubtful "yes": the other lab grades too. An Area's risk is a false
            # pick, not a miss, so the less inclusive Fit stands.
            check = opinions["check"] = await _fit(gateway, "check", d, refused)
            standing = max((judge, check), key=lambda o: o.fit.rank)
            return _verdict(d, opinions, standing, split=judge.fit != check.fit, escalated=True)
        case Depth.FIELD:
            alone = await _fit(gateway, "field_grade", d, refused)
            return _verdict(d, {"field_grade": alone}, alone)
    raise AssertionError(d.topic.depth)


async def _one(d: Due, gateway: Gateway, settings: Grading) -> Graded:
    # A side refused on an earlier run stays refused while its list refused this very question:
    # a changed topic or list asks it again.
    asked = request(d.paper, d.topic, d.name)
    refused = {
        r for r in d.refused if await gateway.refused_throughout(r, asked, paper_id=d.paper.id)
    }
    try:
        return Graded(d, await _graded(d, gateway, settings, refused))
    except CallError as exc:
        # Truncated or invalid: no verdict, and a failed run that counts. A grader that did not
        # answer (a timeout, a provider down) is retried without counting. A grader whose every
        # model refused stops the grading at once (`_graded` raises it only when it does).
        stops = exc.kind == "refused"
        return Graded(
            d, error=str(exc), unusable=exc.unusable, refused=frozenset(refused), stops=stops
        )


def grade(
    due: list[Due], gateway: Gateway, settings: Grading | None = None, concurrency: int = 8
) -> AbstractAsyncContextManager[AsyncIterator[Graded]]:
    """Grade each paper for its topic, giving results as they arrive, held with `async with`
    (`concurrency.bounded`: leaving it stops every call). Writes nothing. A grading whose
    graders fail is given with its error and stops nothing else."""
    chosen = settings or Grading()
    return bounded(due, lambda d: _one(d, gateway, chosen), gateway.width(concurrency))


async def due(
    conn: psycopg.AsyncConnection, tree: Taxonomy, runs: int, limit: int | None = None
) -> tuple[list[Due], list[tuple[str, int]]]:
    """The gradings due, for at most `limit` papers (`store.candidates`), and the (paper,
    revision) whose grading pass they make, to record with `store.passed` once they are
    recorded. A paper's first pass on its current text grades each active topic it leads to
    (`core.grading.grading_topics`); after that only a grading that failed is tried again, and
    a topic a miss was reported for is graded whatever classification said (feedback.md). A
    topic the paper leads to later waits for a re-run (pipeline.md, section 10), as does a
    topic edit, whatever the topic's version. First passes first, oldest paper first, and new
    gradings before failed ones; after `runs` failed runs a grading is not tried at all: it
    waits for the owner. A failed one carries its failed runs and the side refused so far. A
    paused or draft topic grades nothing."""
    active = {t.slug: t for t in tree.topics if t.status == Status.ACTIVE}
    found: list[tuple[str, str, bool]] = []  # (paper, topic, failed before)
    passes: list[tuple[str, int]] = []
    failed_runs: dict[tuple[str, str], int] = {}
    refused: dict[tuple[str, str], list[str]] = {}
    for row in await store.candidates(conn, runs, limit):
        failed = row["failed"]
        failed_runs |= {(row["paper_id"], t): n for t, n in failed.items()}
        refused |= {(row["paper_id"], t): r for t, r in row["refused"].items()}
        if row["passed"]:
            should = set(failed)
        else:
            should = grading_topics(tree, row["memberships"], row["held"])
            passes.append((row["paper_id"], row["revision"]))
        should |= set(row["missed"])
        for slug in sorted(should & active.keys() - set(row["graded"])):
            if failed.get(slug, 0) < runs:
                found.append((row["paper_id"], slug, slug in failed))
    found.sort(key=lambda f: f[2])  # stable: new gradings first, in paper order
    by_id = {p.id: p for p in await papers_store.get_many(conn, sorted({p for p, _, _ in found}))}
    due = [
        Due(
            by_id[p],
            active[s],
            tree.get(s).name,
            failed_runs.get((p, s), 0),
            frozenset(refused.get((p, s), ())),
        )
        for p, s, _ in found
        if p in by_id
    ]
    return due, passes


async def record(conn: psycopg.AsyncConnection, g: Graded) -> bool:
    """Store one grading with `verdict.recorded` and settle the paper (its effective grade,
    rarity and library state follow); or record its failure. Returns False when there was
    nothing to record: the paper's text changed meanwhile, it was dismissed, or the topic is
    no longer active (paused during the call: a paused topic grades nothing)."""
    paper, slug = g.due.paper, g.due.topic.slug
    async with word.change(conn, paper.id, cause=Cause.VERDICT):
        if not await papers_store.still_current(conn, paper):
            return False
        if slug not in {t.slug for t in await taxonomy_store.topics(conn, Status.ACTIVE)}:
            return False
        if g.verdict is None:
            error = g.error or "no answer"
            await failures.failed(
                conn,
                paper,
                "grading",
                error,
                counts=g.unusable,
                topic=slug,
                refused=g.refused,
                stops=g.stops,
            )
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
            one_lab=stored.one_lab,
        )
    return True
