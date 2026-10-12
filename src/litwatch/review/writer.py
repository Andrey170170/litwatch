"""The writer: litwatch's own words for the reading side. A take per (paper, Watch topic),
and the lede that opens an issue. Pure request builders and calls; nothing is stored here."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from litwatch.core import Grade, Opinion, Paper, Topic
from litwatch.llm import Gateway, Request
from litwatch.screening.triage import render_paper

VOICE = """\
You write for litwatch, which reads every new arXiv paper in a field so that one researcher \
does not have to. The researcher follows a narrow research question and reads what you write \
the way one reads a good newspaper: quickly, trusting it to be specific and honest.

Write about the papers, never to the reader: no "you", no "for your question", no audience \
("for monitor builders", "for practitioners"), and no verdict on whether to read it ("required \
reading", "worth reading", "a must"). State what a paper does and shows, in its own terms; \
importance comes through in what you choose to say. Never mention grades, ratings, scores, \
relevance labels, models, graders, reviewers, a pipeline, or that anything was selected, \
flagged or judged. Do not hype, and do not hedge with boilerplate. Use plain sentences. Answer \
with the structured output only."""

TAKE = """{voice}

Write the take on one paper read for the research question below: what the paper does, and \
what it shows that bears on the question, stated as findings rather than as advice. {length} \
Do not start with "This paper" or repeat the title. Where the paper only touches the question, \
say which part of it does.

# The question: {name}

{subject}"""

LENGTHS = {
    Grade.MUST_READ: (
        "Two or three short sentences, at most 70 words; the last may say what follows from "
        "the result."
    ),
    Grade.RELEVANT: "One sentence, at most 35 words.",
}

LEDE = """{voice}

Write the lede of today's issue: what a newspaper prints above its first story. One or two \
short sentences, at most 35 words in all, saying what is in the issue: the topics, the number \
of papers in words, and a few words on the strongest entry. Do not summarise any paper; the \
takes below the lede do that. Lead entries are the day's most important papers. When notes on the day in a field follow, end with a few words on that field too."""


class TakeOutput(BaseModel):
    take: str = Field(description="The take, as it will be printed")


class LedeOutput(BaseModel):
    lede: str = Field(description="The lede, as it will be printed")


def take_request(
    paper: Paper, topic: Topic, grade: Grade, opinions: list[Opinion] | None = None
) -> Request:
    """The request for one take. `opinions` are notes from whoever read the paper for the
    topic before; the writer may use their observations, never their verdicts."""
    user = render_paper(paper)
    if opinions:
        notes = "\n".join(f"- {o.reason}" for o in opinions)
        user += f"\n\nNotes from earlier readers, for your information only:\n{notes}"
    return Request(
        system=TAKE.format(
            voice=VOICE, length=LENGTHS[grade], name=topic.name, subject=topic.subject
        ),
        user=user,
        output=TakeOutput,
    )


@dataclass(frozen=True)
class Entry:
    """One paper as the lede sees it."""

    topic: str  # the topic's name
    title: str
    take: str | None
    lead: bool


def lede_request(
    entries: list[Entry],
    folds: dict[str, int] | None = None,
    notes: list[tuple[str, str]] | None = None,
) -> Request:
    """`notes` are the day's field reviews, as (topic name, review text)."""
    lines = []
    for topic in dict.fromkeys(e.topic for e in entries):
        lines.append(f"## {topic}")
        for e in (e for e in entries if e.topic == topic):
            kind = "lead" if e.lead else "brief"
            lines.append(f"- ({kind}) {e.title}" + (f": {e.take}" if e.take else ""))
    if folds:
        near = ", ".join(f"{n} near {topic}" for topic, n in folds.items() if n)
        if near:
            lines.append(f"\nAlso noted, not in the issue: {near}.")
    for topic, text in notes or []:
        lines.append(f"\n## {topic} (the day in the field)\n{text[:600]}")
    return Request(system=LEDE.format(voice=VOICE), user="\n".join(lines), output=LedeOutput)


async def take(
    gateway: Gateway,
    paper: Paper,
    topic: Topic,
    grade: Grade,
    opinions: list[Opinion] | None = None,
    role: str = "writer",
) -> str:
    """Write one take. Raises `CallError` when no usable text came back."""
    request = take_request(paper, topic, grade, opinions)
    answer = await gateway.ask(role, request, paper_id=paper.id)
    return answer.value.take.strip()


async def lede(
    gateway: Gateway,
    entries: list[Entry],
    folds: dict[str, int] | None = None,
    notes: list[tuple[str, str]] | None = None,
) -> str:
    answer = await gateway.ask("writer", lede_request(entries, folds, notes))
    return answer.value.lede.strip()
