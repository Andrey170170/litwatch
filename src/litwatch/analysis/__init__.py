"""Analysis (docs/specs/v1/pipeline.md, section 3): one call per paper to the `analyse` role, from
title and abstract, for the Gist, Weight in the paper's primary class with one sentence why, and
the facets. `analyse` asks and writes nothing; `record` stores what it found."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

import psycopg
from pydantic import BaseModel, Field, field_validator

from litwatch import events
from litwatch.concurrency import bounded
from litwatch.core import Analysis, Cause, Contribution, Facets, Paper, TaxonomyClass, Weight
from litwatch.core.analysis import WEIGHT_DEFINITIONS
from litwatch.feedback import word
from litwatch.llm import CallError, Gateway, Request, Usage
from litwatch.store import analyses as store
from litwatch.store import failures
from litwatch.store import papers as papers_store
from litwatch.writing import READER, VOICE

ROLE = "analyse"
# The Gist asked for is 20–30 words. The accepted range is a guard against a broken answer (a
# fragment, the abstract restated), not a second target: outside it the answer fails like any
# invalid one. At 35 it failed 2.7 % of the two-week run's answers and left 11 papers with no
# analysis; the failed replies were not kept, but asked again those papers' Gists ran long (up
# to 42 words), never short (evals/2026-10-15-tuning.md).
GIST_WORDS = (15, 45)

SYSTEM = f"""\
You read the title and abstract of one arXiv paper and describe it for litwatch, which reads \
every new paper in a field. Answer with the structured output only.

{VOICE}

{READER}

gist: what the paper does and its main result, in one or two short sentences of 20 to 30 words \
in all. 30 words is a hard limit: count the words before answering, and cut detail rather than \
go over; a name the reader cannot know is the first detail to cut. It is about the paper, not \
about any area or topic. Do not start with "This paper" and do not repeat the title.

contribution: the main kind of contribution the paper makes.
- method: a new method, model, algorithm or system
- analysis: an empirical study or analysis of existing methods or phenomena
- benchmark: a benchmark or dataset
- theory: theoretical results
- position: a position or perspective
- survey: a survey or review

releases_code, releases_weights: whether the abstract announces released code, and released \
model weights. Only what the abstract says counts; when it says nothing, false.

weight: how much the paper matters in its own area, which is given with the paper. Judge it \
against that area alone: what is a landmark in one area can be incremental in another. Judge \
from what the abstract shows, and give one sentence why in weight_reason. The levels:
{chr(10).join(f"- {w.value}: {meaning}" for w, meaning in WEIGHT_DEFINITIONS.items())}"""


class Output(BaseModel):
    """The answer's schema. The reason comes before the Weight it argues for."""

    gist: str = Field(description="20 to 30 words, never more than 30")
    contribution: Contribution
    releases_code: bool
    releases_weights: bool
    weight_reason: str = Field(description="One sentence")
    weight: Weight

    @field_validator("gist")
    @classmethod
    def _length(cls, gist: str) -> str:
        gist = " ".join(gist.split())
        low, high = GIST_WORDS
        if not low <= len(gist.split()) <= high:
            raise ValueError(f"a Gist of {len(gist.split())} words")
        return gist

    def analysis(self) -> Analysis:
        return Analysis(
            gist=self.gist,
            weight=self.weight,
            weight_reason=self.weight_reason,
            facets=Facets(
                contribution=self.contribution,
                releases_code=self.releases_code,
                releases_weights=self.releases_weights,
            ),
        )


def request(paper: Paper, area: TaxonomyClass) -> Request:
    """The instructions repeat across papers, so they are cached; the area goes with the paper."""
    user = (
        f"Area: {area.name}: {area.description}\n\n"
        f"Title: {paper.title}\n\nAbstract:\n{paper.abstract}"
    )
    return Request(system=SYSTEM, user=user, output=Output)


@dataclass
class Analysed:
    """One paper's analysis against its primary class, or why there is none."""

    paper: Paper
    primary: str
    analysis: Analysis | None = None
    model: str | None = None
    usage: Usage | None = None
    error: str | None = None
    unusable: bool = False  # the model answered unusably: a failed run that counts
    refused: bool = False  # every model of the role refused: the step stops at once


async def _one(paper: Paper, area: TaxonomyClass, failed_runs: int, gateway: Gateway) -> Analysed:
    try:
        answer = await gateway.ask(ROLE, request(paper, area), paper_id=paper.id, start=failed_runs)
    except CallError as exc:
        return Analysed(
            paper,
            area.slug,
            error=str(exc),
            usage=exc.usage,
            unusable=exc.unusable,
            refused=exc.kind == "refused",
        )
    return Analysed(paper, area.slug, answer.value.analysis(), answer.model, answer.usage)


def analyse(
    papers: list[tuple[Paper, TaxonomyClass, int]], gateway: Gateway, concurrency: int = 8
) -> AbstractAsyncContextManager[AsyncIterator[Analysed]]:
    """Ask the `analyse` role about each paper, judged in its primary class, from as far down
    the role's list as its failed runs say (D4), giving results as they arrive, held with
    `async with` (`concurrency.bounded`: leaving it stops every call). Writes nothing. A failed
    call is given with its error."""
    return bounded(papers, lambda pa: _one(*pa, gateway), gateway.width(concurrency))


async def record(conn: psycopg.AsyncConnection, a: Analysed) -> bool:
    """Store one paper's analysis, its Weight and its Gist from the abstract, with
    `gist.written` when that Gist is current, and settle the paper (its grades and rarity
    follow the Weight); or record its failure. Returns False when there was nothing to record:
    the paper's text changed meanwhile, or it was dismissed."""
    async with word.change(conn, a.paper.id, cause=Cause.VERDICT):
        if not await papers_store.still_current(conn, a.paper):
            return False
        if a.analysis is None or a.model is None:
            error = a.error or "no answer"
            refused = [ROLE] if a.refused else []
            await failures.failed(
                conn,
                a.paper,
                "analysis",
                error,
                counts=a.unusable,
                refused=refused,
                stops=a.refused,
            )
            return True
        gist = await store.add(conn, a.paper, a.primary, a.analysis, a.model)
        await failures.succeeded(conn, a.paper.id, "analysis")
        # A Gist the full text already gave this revision stays current.
        if await store.current_gist_id(conn, a.paper.id) == gist:
            await events.emit(
                conn,
                events.GIST_WRITTEN,
                a.paper.id,
                revision=a.paper.revision,
                source="abstract",
                gist_id=gist,
            )
    return True
