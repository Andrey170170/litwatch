"""One topic and one paper per call, with the rubric in context, asked of two labs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from litwatch.core import Grade, Opinion, Paper, Topic, reconcile
from litwatch.llm import CallError, Gateway, Request
from litwatch.screening.triage import render_paper

GRADERS = ("judge", "check")

SYSTEM = """\
You grade one arXiv paper for one research topic that a researcher watches closely. You see \
only the paper's title, abstract and categories. Answer with the structured output only.

The grades:

must_read: the paper is central to the topic's subject and meets its must-read rubric.
relevant: the paper is on the subject, or close to it and important enough to be worth the \
researcher's time, without meeting the must-read rubric.
adjacent: same neighbourhood. The paper overlaps the subject in part, or is a likely source of \
ideas for it, without being about it.
none: everything else.

Words shared with the subject are not membership; "faithful", "trace", "monitor" and \
"reasoning" are used in many unrelated senses. The researcher would rather see a paper one \
grade too high than miss it: when it is genuinely unclear between two grades, choose the more \
inclusive one.

Give a reason of one sentence naming the deciding evidence from the abstract; for none a few \
words suffice.

# Topic: {name}

Subject: {subject}

{rubric}"""

NO_RUBRIC = "This topic has no must-read rubric, so its best grade is relevant."
# Rubrics carried over from 0.x speak of tiers.
TIER_LEGEND = (
    "\n\nWhere the rubric speaks of tiers, tier 1 is must_read, tier 2 is relevant, tier 3 is "
    "adjacent and tier 4 is none."
)


class GradeOutput(BaseModel):
    reason: str = Field(description="One sentence")
    grade: Grade


def grading_request(paper: Paper, topic: Topic) -> Request:
    rubric = NO_RUBRIC
    if topic.rubric:
        rubric = f"Must-read rubric: {topic.rubric}"
        if "tier" in topic.rubric.lower():
            rubric += TIER_LEGEND
    return Request(
        system=SYSTEM.format(name=topic.name, subject=topic.subject, rubric=rubric),
        user=render_paper(paper),
        output=GradeOutput,
    )


@dataclass
class Graded:
    """Both labs' opinions of one paper for one topic. A verdict needs both."""

    paper: Paper
    topic: Topic
    judge: Opinion | None = None
    check: Opinion | None = None
    errors: dict[str, str] = field(default_factory=dict)  # role -> why it gave no opinion
    cost: float = 0.0

    @property
    def complete(self) -> bool:
        return self.judge is not None and self.check is not None

    @property
    def grade(self) -> Grade | None:
        return reconcile(self.judge, self.check)[0] if self.complete else None

    @property
    def split(self) -> bool | None:
        return reconcile(self.judge, self.check)[1] if self.complete else None

    @property
    def error(self) -> str | None:
        return "; ".join(f"{role}: {why}" for role, why in self.errors.items()) or None


async def grade(paper: Paper, topic: Topic, gateway: Gateway) -> Graded:
    """Ask `judge` and `check` concurrently. Writes nothing and never raises for a failed
    call: a slow or failing lab leaves its opinion empty and its reason in `errors`."""
    request = grading_request(paper, topic)
    result = Graded(paper, topic)

    async def ask(role: str) -> None:
        try:
            answer = await gateway.ask(role, request, paper_id=paper.id)
        except CallError as exc:
            result.errors[role] = str(exc)
            return
        grade = answer.value.grade
        if grade == Grade.MUST_READ and not topic.rubric:
            grade = Grade.RELEVANT  # nobody has said what is required reading
        opinion = Opinion(grade=grade, reason=answer.value.reason, model=answer.model)
        setattr(result, role, opinion)
        result.cost += answer.usage.cost or 0.0

    await asyncio.gather(*(ask(role) for role in GRADERS))
    return result


async def grade_many(
    pairs: list[tuple[Paper, Topic]], gateway: Gateway, concurrency: int = 6
) -> AsyncIterator[Graded]:
    """Grade a chosen set, yielding results as they arrive. Candidates are graded
    independently, so one failure stops nothing else."""
    limit = asyncio.Semaphore(concurrency)

    async def bounded(paper: Paper, topic: Topic) -> Graded:
        async with limit:
            return await grade(paper, topic, gateway)

    tasks = [asyncio.create_task(bounded(p, t)) for p, t in pairs]
    try:
        for done in asyncio.as_completed(tasks):
            yield await done
    finally:
        for task in tasks:
            task.cancel()
