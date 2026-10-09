"""Triage: one cheap call per paper that answers for every active topic at once."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, create_model

from litwatch.core import Paper, Topic, TopicKind
from litwatch.llm import CallError, Gateway, Request, Usage

SYSTEM = """\
You sort arXiv papers for a researcher. You see only a paper's title, abstract and categories. \
Answer with the structured output only.

The researcher follows the topics listed below. For each topic, answer one yes-or-no question \
about the paper. Judge each topic on its own: a paper can be yes for several topics or for none, \
and most papers are no for most topics.

Watch topics are the researcher's own research questions. Missing a paper on one of them is \
costly, while a wrong yes costs only a second look by a stronger model. Answer yes if the paper \
could plausibly belong: it is about the topic, overlaps with it in part, or is close enough that \
a careful reader would want to check. Answer no when the paper is clearly about something else. \
Shared vocabulary alone is not enough: "faithful", "trace", "monitor" and "reasoning" are used \
in many unrelated senses. When you are genuinely unsure, answer yes. Give one sentence naming \
the evidence in the abstract that decided it; for a clear no a few words suffice.

Field topics are broad areas the researcher keeps an eye on. Answer yes if the paper is a \
contribution to that field, and no if it only uses the field's tools or mentions it in passing.

{topics}"""


class WatchAnswer(BaseModel):
    why: str = Field(description="One sentence")
    yes: bool


class FieldAnswer(BaseModel):
    yes: bool


def render_topics(topics: list[Topic]) -> str:
    def block(topic: Topic) -> str:
        return f"## {topic.name} (`{topic.field}`)\n{topic.subject}"

    watch = [block(t) for t in topics if t.kind == TopicKind.WATCH]
    fields = [block(t) for t in topics if t.kind != TopicKind.WATCH]
    parts = []
    if watch:
        parts.append("# Watch topics\n\n" + "\n\n".join(watch))
    if fields:
        parts.append("# Field topics\n\n" + "\n\n".join(fields))
    return "\n\n".join(parts)


def output_model(topics: list[Topic]) -> type[BaseModel]:
    """One required field per topic, so the model must answer for every one of them."""
    fields = {
        t.field: (WatchAnswer if t.kind == TopicKind.WATCH else FieldAnswer, ...) for t in topics
    }
    return create_model("Triage", **fields)  # type: ignore[call-overload]


def render_paper(paper: Paper) -> str:
    cats = ", ".join(paper.categories) if paper.categories else "unknown"
    return f"Title: {paper.title}\nCategories: {cats}\n\nAbstract:\n{paper.abstract}"


def triage_request(paper: Paper, topics: list[Topic]) -> Request:
    return Request(
        system=SYSTEM.format(topics=render_topics(topics)),
        user=render_paper(paper),
        output=output_model(topics),
    )


@dataclass
class Triaged:
    """Triage's answers for one paper, or why there are none."""

    paper: Paper
    # topic slug -> (yes, why); `why` is None for field topics.
    answers: dict[str, tuple[bool, str | None]] = field(default_factory=dict)
    model: str | None = None
    usage: Usage | None = None
    error: str | None = None

    def yes(self, slug: str) -> bool:
        return slug in self.answers and self.answers[slug][0]


async def _one(paper: Paper, topics: list[Topic], request: Request, gateway: Gateway) -> Triaged:
    try:
        answer = await gateway.ask("triage", request, paper_id=paper.id)
    except CallError as exc:
        return Triaged(paper, error=str(exc))
    answers = {}
    for topic in topics:
        given = getattr(answer.value, topic.field)
        answers[topic.slug] = (given.yes, getattr(given, "why", None))
    return Triaged(paper, answers, answer.model, answer.usage)


async def triage(
    papers: list[Paper], topics: list[Topic], gateway: Gateway, concurrency: int = 16
) -> AsyncIterator[Triaged]:
    """Ask the `triage` role about each paper and yield results as they arrive.

    Writes nothing. A paper whose call fails is yielded with its error, and stops nothing else.
    """
    if not topics or not papers:
        return
    system = SYSTEM.format(topics=render_topics(topics))
    output = output_model(topics)
    limit = asyncio.Semaphore(concurrency)

    async def bounded(paper: Paper) -> Triaged:
        async with limit:
            request = Request(system=system, user=render_paper(paper), output=output)
            return await _one(paper, topics, request, gateway)

    tasks = [asyncio.create_task(bounded(p)) for p in papers]
    try:
        for done in asyncio.as_completed(tasks):
            yield await done
    finally:
        for task in tasks:
            task.cancel()
