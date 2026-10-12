"""Reviews of Radar and Pulse topics, recaps of Watch topics, and the weekly issue's lede.
Pure request builders and calls; nothing is stored here."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from litwatch.core import Paper, Topic, TopicKind
from litwatch.llm import Gateway, Request
from litwatch.review.writer import VOICE

# A field this large is shortlisted in chunks first, so one call never reads too much.
FULL_REVIEW_LIMIT = 250
CHUNK = 120
SHORTLIST = 15
ABSTRACT_CHARS = 700

SHAPES = {
    (TopicKind.RADAR, "week"): (
        "two to four short paragraphs on what happened in the field this week, the themes and "
        "anything that moved, and about eight picks",
        8,
    ),
    (TopicKind.RADAR, "day"): (
        "one or two sentences on anything that stands out today, or that it was a quiet day; no "
        "themes forced onto one day's papers; up to five picks",
        5,
    ),
    (TopicKind.PULSE, "week"): (
        "one paragraph on what is going on in the field, and up to three picks",
        3,
    ),
}

REVIEW = """{voice}

You review the new arXiv papers of one {span} in a field the researcher follows for its \
bearing on their work, without reading it closely. Write {shape}. Each pick names a paper from \
the list by its id, with one sentence on what it shows or adds to the field. Pick fewer when \
fewer deserve it. Name papers in the text by their idea, never by their id.

# The field: {name}

{subject}
{interests}"""

SHORTLIST_PROMPT = """{voice}

From the list, choose up to {n} papers most worth the attention of a researcher who follows \
this field for its bearing on their work. Give their ids only.

# The field: {name}

{subject}
{interests}"""

RECAP = """{voice}

Write the week's recap for one research question: two to four sentences on what this week's \
papers were about and any thread running through them, from the takes below. Do not list the \
papers; they are listed under the recap.

# The question: {name}

{subject}"""

WEEKLY_LEDE = """{voice}

Write the lede of the weekly issue: one or two short sentences, at most 40 words, saying what \
the week brought across the topics below. Do not summarise any paper."""


class Pick(BaseModel):
    id: str
    why: str = Field(description="One sentence")


class ReviewOutput(BaseModel):
    text: str = Field(description="The review's prose, as it will be printed")
    picks: list[Pick]


class ShortlistOutput(BaseModel):
    ids: list[str]


class RecapOutput(BaseModel):
    recap: str


class LedeOutput(BaseModel):
    lede: str


def _listing(papers: list[Paper]) -> str:
    return "\n\n".join(f"[{p.id}] {p.title}\n{p.abstract[:ABSTRACT_CHARS]}" for p in papers)


def _interests(topic: Topic) -> str:
    return f"\nWhat the researcher cares about here: {topic.interests}" if topic.interests else ""


def review_request(topic: Topic, period: str, papers: list[Paper]) -> Request:
    shape, _ = SHAPES[(topic.kind, period)]
    return Request(
        system=REVIEW.format(
            voice=VOICE,
            span="day" if period == "day" else "week",
            shape=shape,
            name=topic.name,
            subject=topic.subject,
            interests=_interests(topic),
        ),
        user=_listing(papers),
        output=ReviewOutput,
    )


@dataclass
class Written:
    text: str
    picks: list[Pick]


async def review(gateway: Gateway, topic: Topic, period: str, papers: list[Paper]) -> Written:
    """Review the period's member papers. A field too large for one call is shortlisted in
    chunks first. Picks outside the list are dropped, and kept to the shape's number."""
    pool = papers
    if len(papers) > FULL_REVIEW_LIMIT:
        chosen: set[str] = set()
        for start in range(0, len(papers), CHUNK):
            chunk = papers[start : start + CHUNK]
            # Each chunk's share of the final call, so a huge week still fits in one.
            n = max(1, min(SHORTLIST, FULL_REVIEW_LIMIT * len(chunk) // len(papers)))
            request = Request(
                system=SHORTLIST_PROMPT.format(
                    voice=VOICE,
                    n=n,
                    name=topic.name,
                    subject=topic.subject,
                    interests=_interests(topic),
                ),
                user=_listing(chunk),
                output=ShortlistOutput,
            )
            answer = await gateway.ask("review", request)
            ids = {p.id for p in chunk}
            chosen |= set([i for i in dict.fromkeys(answer.value.ids) if i in ids][:n])
        pool = [p for p in papers if p.id in chosen]
        if not pool:
            # Nothing came back worth a look: the final call still reads the field, as an
            # even sample, so a quiet field is reviewed as quiet rather than unread.
            n = min(len(papers), FULL_REVIEW_LIMIT)
            pool = [papers[i * len(papers) // n] for i in range(n)]
    answer = await gateway.ask("review", review_request(topic, period, pool))
    known = {p.id for p in pool}
    _, most = SHAPES[(topic.kind, period)]
    picks, seen = [], set()
    for pick in answer.value.picks:
        if pick.id in known and pick.id not in seen:
            seen.add(pick.id)
            picks.append(pick)
    return Written(answer.value.text.strip(), picks[:most])


@dataclass(frozen=True)
class RecapEntry:
    title: str
    take: str | None


async def recap(gateway: Gateway, topic: Topic, entries: list[RecapEntry]) -> str:
    lines = [f"- {e.title}" + (f": {e.take}" if e.take else "") for e in entries]
    request = Request(
        system=RECAP.format(voice=VOICE, name=topic.name, subject=topic.subject),
        user="\n".join(lines),
        output=RecapOutput,
    )
    return (await gateway.ask("writer", request)).value.recap.strip()


async def weekly_lede(gateway: Gateway, sections: list[tuple[str, str]]) -> str:
    """`sections` are (topic name, its recap or review text)."""
    user = "\n\n".join(f"## {name}\n{text[:600]}" for name, text in sections)
    request = Request(system=WEEKLY_LEDE.format(voice=VOICE), user=user, output=LedeOutput)
    return (await gateway.ask("writer", request)).value.lede.strip()
