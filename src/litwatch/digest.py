"""Digests: one general digest across the feed plus one per flagged selection, per run.

The general digest is the whole day in one page: an overview, then a section per selection with
its must-read and top recommended papers; a paper in several selections is placed under the one
where it ranks highest. A selection digest covers every member of that selection in more depth.

The model returns structured notes keyed by arXiv id; the markdown is rendered here, so titles
and links are never the model's to get wrong. Markdown stays within the subset the Notion
projection renders: `**bold**` lines, `- ` bullets, `[title](url)` links.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from litwatch.models import Usage
from litwatch.screen import usage_of
from litwatch.selection import Selection

log = logging.getLogger(__name__)

GENERAL = "general"
GENERAL_TITLE = "General"
RECOMMENDED_CAP = 8  # tier 2 bullets per selection digest; the rest are counted
GENERAL_RECOMMENDED_CAP = 5  # tier 2 bullets per section of the general digest
EMPTY_DIGEST = "No new member papers in this run."


class Note(BaseModel):
    id: str = Field(description="The paper's arXiv id exactly as given")
    text: str


class SelectionOutput(BaseModel):
    overview: str = Field(description="Two or three sentences on what today adds to the subject")
    must_read: list[Note] = Field(description="Every tier 1 paper, two or three sentences each")
    recommended: list[Note] = Field(
        description=f"Up to {RECOMMENDED_CAP} tier 2 papers, one sentence each, most notable first"
    )


class Section(BaseModel):
    slug: str = Field(description="The selection's slug exactly as given")
    overview: str = Field(description="One or two sentences on what today adds to the selection")
    must_read: list[Note] = Field(
        description="Every tier 1 paper of the section, one or two sentences each"
    )
    recommended: list[Note] = Field(
        description=f"Up to {GENERAL_RECOMMENDED_CAP} tier 2 papers, one sentence each, "
        "most notable first"
    )


class GeneralOutput(BaseModel):
    overview: str = Field(description="Three or four sentences across the whole feed")
    sections: list[Section] = Field(description="One per selection given, in the order given")


SELECTION_PROMPT = f"""You write the daily digest for one selection of an NLP research lab's \
literature watch: a topic one member owns, with a subject and, for some, a tier 1 rubric. You \
are given the selection and today's papers: tier 1 (must read) with a reading card written from \
the full text, and tier 2 (recommended) with the abstract and the screener's reason.

Return JSON. `overview`: two or three sentences on what today adds to the subject, naming no \
more than two papers. `must_read`: one entry per tier 1 paper, all of them, two or three \
sentences each: what it contributes, by what method, against what ground truth or evidence; \
where the card names a detection method or ground truth, center on that. `recommended`: at most \
{RECOMMENDED_CAP} tier 2 papers, the most notable first, one sentence each on what the paper \
does. Use each paper's arXiv id as `id`, exactly as given, and never mention arXiv ids in \
prose; in the overview refer to a paper by a short name from its title. Do not repeat titles \
inside the notes; the reader sees the title next to each. Be concrete about methods and \
claims, do not hype, do not speculate beyond what you are given."""

GENERAL_PROMPT = f"""You write the daily digest of an NLP research lab's literature watch: one \
page a reader spends five to ten minutes on to get the hang of the day. The lab follows several \
selections, each a topic with a subject and, for some, a tier 1 rubric. You are given, per \
selection, today's papers placed there: tier 1 (must read) with a reading card written from the \
full text, and tier 2 (recommended) with the abstract and the screener's reason.

Return JSON. `overview`: three or four sentences across the whole feed on what stands out today \
and where the volume is, naming no more than three papers. `sections`: one per selection given, \
in the order given, with its slug exactly as given. In each: `overview`, one or two sentences on \
what today adds to that subject; `must_read`, one entry per tier 1 paper, all of them, one or \
two sentences each on what it contributes and by what method; `recommended`, at most \
{GENERAL_RECOMMENDED_CAP} tier 2 papers, the most notable first, one sentence each on what the \
paper does. Use each paper's arXiv id as `id`, exactly as given, and never mention arXiv ids in \
prose; refer to a paper by a short name from its title. Do not repeat titles inside the notes; \
the reader sees the title next to each. Be concrete about methods and claims, do not hype, do \
not speculate beyond what you are given."""


@dataclass
class Digest:
    source: str  # GENERAL or a selection slug
    title: str  # GENERAL_TITLE or the selection name
    body: str  # markdown
    model: str | None = None
    cost: float | None = None


# -- grouping and prompt inputs --------------------------------------------------------------------


def by_tier(selection: Selection, papers: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Member rows of one selection keyed by its tier there (1..3)."""
    grouped: dict[int, list[dict[str, Any]]] = {1: [], 2: [], 3: []}
    for row in papers:
        verdict = (row.get("verdicts") or {}).get(selection.slug)
        if verdict and verdict["tier"] in grouped:
            grouped[verdict["tier"]].append(row)
    return grouped


Groups = dict[str, dict[int, list[dict[str, Any]]]]  # slug -> tier -> rows


def assign(selections: list[Selection], papers: list[dict[str, Any]]) -> Groups:
    """Each paper under one selection only: where its tier is lowest, earlier selection on ties."""
    owned: Groups = {s.slug: {1: [], 2: [], 3: []} for s in selections}
    for row in papers:
        verdicts = row.get("verdicts") or {}
        best = None
        for s in selections:
            v = verdicts.get(s.slug)
            if v and v["tier"] in (1, 2, 3) and (best is None or v["tier"] < best[1]):
                best = (s.slug, v["tier"])
        if best:
            owned[best[0]][best[1]].append(row)
    return owned


def _card_lines(row: dict[str, Any]) -> list[str]:
    card = row.get("enrichment")
    if not card:
        return [f"Abstract: {(row.get('abstract') or '').strip()}"]
    lines = [f"Key claim: {card['key_claim']}", f"Summary: {card['summary']}"]
    if faith := card.get("faithfulness"):
        lines += [
            f"Mismatch detected: {', '.join(faith['mismatch']) or 'none'}",
            f"Ground truth: {faith['ground_truth']}",
            f"Detection method: {faith['detection_method']}",
            f"Generality: {faith['generality']}",
        ]
    return lines


def render_selection_input(
    selection: Selection, groups: dict[int, list[dict[str, Any]]], with_slug: bool = False
) -> str:
    head = f"# {selection.name} (slug: {selection.slug})" if with_slug else f"# {selection.name}"
    lines = [head, f"Subject: {selection.subject}"]
    if selection.rubric:
        lines.append(f"Tier 1 rubric: {selection.rubric}")
    for tier in (1, 2):
        for row in groups[tier]:
            lines += ["", f"## Tier {tier}: {row['title']} (arXiv {row['id']})"]
            lines.append(f"Reason: {row['verdicts'][selection.slug]['reason']}")
            if tier == 1:
                lines += _card_lines(row)
            else:
                lines.append(f"Abstract: {(row.get('abstract') or '').strip()}")
    return "\n".join(lines)


def render_general_input(selections: list[Selection], owned: Groups) -> str:
    """One block per selection that owns a tier 1 or 2 paper today."""
    blocks = [
        render_selection_input(s, owned[s.slug], with_slug=True)
        for s in selections
        if owned[s.slug][1] or owned[s.slug][2]
    ]
    return "\n\n---\n\n".join(blocks)


# -- markdown rendering ----------------------------------------------------------------------------


def _link(row: dict[str, Any]) -> str:
    title = " ".join(str(row["title"]).split())
    return f"[{title}]({row.get('url') or f'https://arxiv.org/abs/{row["id"]}'})"


def _bullets(notes: list[Note], rows: list[dict[str, Any]], suffix=lambda r: "") -> list[str]:
    """Bullets in the model's order, unknown ids dropped, listed papers never omitted."""
    by_id = {r["id"]: r for r in rows}
    seen: set[str] = set()
    out = []
    for note in notes:
        if (row := by_id.get(note.id)) and note.id not in seen:
            seen.add(note.id)
            out.append(f"- {_link(row)}{suffix(row)} — {' '.join(note.text.split())}")
    return out


def _all_bullets(notes: list[Note], rows: list[dict[str, Any]], suffix=lambda r: "") -> list[str]:
    """Every row gets a bullet: tier 1 papers must appear even when the model skipped one."""
    out = _bullets(notes, rows, suffix)
    covered = {n.id for n in notes}
    for row in rows:
        if row["id"] not in covered:
            card = row.get("enrichment") or {}
            fallback = card.get("key_claim") or "See the reading card."
            out.append(f"- {_link(row)}{suffix(row)} — {fallback}")
    return out


def render_selection_digest(
    selection: Selection, groups: dict[int, list[dict[str, Any]]], out: SelectionOutput | None
) -> str:
    """The selection's page: overview, must-read bullets, recommended bullets, the rest counted."""
    t1, t2, t3 = groups[1], groups[2], groups[3]
    parts: list[str] = []
    if out is not None:
        parts.append(" ".join(out.overview.split()))
        if t1:
            parts.append("**Must read**\n" + "\n".join(_all_bullets(out.must_read, t1)))
        if t2:
            bullets = _bullets(out.recommended[:RECOMMENDED_CAP], t2)
            parts.append("**Recommended**\n" + "\n".join(bullets))
            rest = len(t2) - len(bullets)
        else:
            rest = 0
    else:
        rest = len(t2)
    tail = []
    if rest:
        tail.append(f"{rest} more recommended")
    if t3:
        tail.append(f"{len(t3)} good to know")
    if tail:
        parts.append(
            f"Plus {' and '.join(tail)} in the Papers table (filter Selections = {selection.name})."
        )
    return "\n\n".join(parts) if parts else EMPTY_DIGEST


def _also(selections: list[Selection], row: dict[str, Any], owner: str) -> str:
    """Suffix naming the other selections where the paper is tier 1 or 2."""
    verdicts = row.get("verdicts") or {}
    others = [
        s.name
        for s in selections
        if s.slug != owner and (verdicts.get(s.slug) or {}).get("tier", 4) <= 2
    ]
    return f" (also {', '.join(others)})" if others else ""


def render_general_digest(
    selections: list[Selection], owned: Groups, out: GeneralOutput | None
) -> str:
    """The whole day: overview, then per selection a header with counts, its overview, bullets."""
    sections = {sec.slug: sec for sec in out.sections} if out is not None else {}
    parts: list[str] = []
    if out is not None:
        parts.append(" ".join(out.overview.split()))
    for s in selections:
        t1, t2, t3 = owned[s.slug][1], owned[s.slug][2], owned[s.slug][3]
        if not (t1 or t2 or t3):
            continue
        sec = sections.get(s.slug)
        head = f"**{s.name}** · {len(t1)} must read, {len(t2)} recommended, {len(t3)} good to know."
        if sec is not None:
            head += " " + " ".join(sec.overview.split())
        lines = [head]
        suffix = lambda r, owner=s.slug: _also(selections, r, owner)  # noqa: E731
        if t1:
            lines += _all_bullets(sec.must_read if sec else [], t1, suffix)
        if t2 and sec is not None:
            lines += _bullets(sec.recommended[:GENERAL_RECOMMENDED_CAP], t2, suffix)
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else EMPTY_DIGEST


# -- the model calls -------------------------------------------------------------------------------

FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def unfence(text: str) -> str:
    """The JSON inside a ```json fence, or the text as is."""
    m = FENCE.match(text)
    return m.group(1) if m else text.strip()


class Digester:
    """Writes the run's digests: general first in the result, then one per flagged selection."""

    def __init__(self, client: AsyncOpenAI, model: str, effort: str = "low") -> None:
        self.client, self.model, self.effort = client, model, effort

    async def _ask(self, system: str, user: str, schema: type[BaseModel]) -> tuple[Any, Usage]:
        """Structured output the lenient way: writing models wrap JSON in fences now and then."""
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
        }
        last: Exception | None = None
        for _attempt in range(2):
            completion = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=response_format,
                extra_body={"reasoning": {"effort": self.effort}, "usage": {"include": True}},
            )
            text = completion.choices[0].message.content or ""
            try:
                return schema.model_validate_json(unfence(text)), usage_of(completion, self.model)
            except ValueError as exc:
                last = exc
                log.warning("%s digest output rejected, retrying: %s", self.model, exc)
        raise RuntimeError(f"{self.model} returned no parsable digest: {last}")

    async def write_selection(
        self, selection: Selection, groups: dict[int, list[dict[str, Any]]]
    ) -> Digest:
        """No model call when there is nothing above tier 3."""
        if not (groups[1] or groups[2]):
            return Digest(
                selection.slug, selection.name, render_selection_digest(selection, groups, None)
            )
        out, usage = await self._ask(
            SELECTION_PROMPT, render_selection_input(selection, groups), SelectionOutput
        )
        body = render_selection_digest(selection, groups, out)
        return Digest(selection.slug, selection.name, body, usage.model, usage.cost)

    async def write_general(self, selections: list[Selection], owned: Groups) -> Digest:
        """No model call when nothing above tier 3 was found anywhere."""
        rendered = render_general_input(selections, owned)
        if not rendered:
            return Digest(GENERAL, GENERAL_TITLE, render_general_digest(selections, owned, None))
        out, usage = await self._ask(GENERAL_PROMPT, rendered, GeneralOutput)
        body = render_general_digest(selections, owned, out)
        return Digest(GENERAL, GENERAL_TITLE, body, usage.model, usage.cost)

    async def write_all(
        self, selections: list[Selection], papers: list[dict[str, Any]]
    ) -> list[Digest]:
        """General digest plus one per selection with `digest` set and at least one member."""
        groups = {s.slug: by_tier(s, papers) for s in selections}
        flagged = [s for s in selections if s.digest and any(groups[s.slug].values())]
        results = await asyncio.gather(
            self.write_general(selections, assign(selections, papers)),
            *(self.write_selection(s, groups[s.slug]) for s in flagged),
            return_exceptions=True,
        )
        digests: list[Digest] = []
        for source, result in zip([GENERAL, *(s.slug for s in flagged)], results, strict=True):
            if isinstance(result, BaseException):
                # One digest failing must not lose the others or the run; it is rewritten by hand.
                log.error("digest %s failed: %s", source, result)
            else:
                digests.append(result)
        return digests
