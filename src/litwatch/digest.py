"""Digests: one general digest across the feed plus one per flagged selection, per run.

The general digest is the whole day in one page: an overview, then a section per selection with
its must-read and top recommended papers; a paper in several selections is placed under the one
where it ranks highest. A selection digest covers every member of that selection in more depth.

A paper first posted well before we first fetched it (a revision or a late cross-listing of
older work) still goes through screening and the curated pipeline, but is labelled as such: tier 1
with a badge, tier 2 in a section of its own. Covered tier 1/2 papers with a newer version get a
dated note, once per version.

The model returns structured notes keyed by arXiv id; the markdown is rendered here, so titles
and links are never the model's to get wrong. Markdown stays within the subset the Notion
projection renders: `**bold**` lines, `- ` bullets, `[title](url)` links.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from litwatch.calls import current_journal, recorded_completion
from litwatch.models import Usage
from litwatch.routing import provider_preferences
from litwatch.selection import Selection

log = logging.getLogger(__name__)

GENERAL = "general"
GENERAL_TITLE = "General"
RECOMMENDED_CAP = 8  # tier 2 bullets per selection digest; the rest are counted
GENERAL_RECOMMENDED_CAP = 5  # tier 2 bullets per section of the general digest
EMPTY_DIGEST = "No new member papers in this run."
TIER3_CAP = 40  # tier 3 titles shown to the model per selection digest
UPDATED_CAP = 5  # older tier 2 bullets per digest; the rest are counted
OLDER_INPUT_CAP = 30  # older tier 2 titles shown to the model per digest
OLDER_AFTER = timedelta(days=7)  # first posted this long before we first fetched it
UPDATED_TITLE = "Older papers, newly updated"


class Note(BaseModel):
    id: str = Field(description="The paper's arXiv id exactly as given")
    text: str


class SelectionOutput(BaseModel):
    overview: str = Field(
        description="Two or three sentences on what today adds to the subject; when nothing is "
        "above tier 3, one or two sentences on what the tier 3 papers touch on"
    )
    must_read: list[Note] = Field(description="Every tier 1 paper, two or three sentences each")
    recommended: list[Note] = Field(
        description=f"Up to {RECOMMENDED_CAP} tier 2 papers, one sentence each, most notable first"
    )
    updated: list[Note] = Field(
        default_factory=list,
        description=f"Up to {UPDATED_CAP} of the older papers newly updated, one sentence each, "
        "most notable first",
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
    updated: list[Note] = Field(
        default_factory=list,
        description=f"Up to {UPDATED_CAP} of the older papers newly updated, one sentence each, "
        "most notable first",
    )


SELECTION_PROMPT = f"""You write the daily digest for one selection of an NLP research lab's \
literature watch: a topic one member owns, with a subject and, for some, a tier 1 rubric. You \
are given the selection and today's papers: tier 1 (must read) with a reading card written from \
the full text, tier 2 (recommended) with the abstract and the screener's reason, and tier 3 \
(good to know) as a title and the screener's reason each.

Return JSON. `overview`: two or three sentences on what today adds to the subject, naming no \
more than two papers; tier 3 papers inform the overview but are not listed. When nothing is \
above tier 3, the overview is one or two sentences on what the tier 3 papers touch on and why \
none of them meets the rubric, and `must_read` and `recommended` are empty. `must_read`: one \
entry per tier 1 paper, all of them, two or three \
sentences each: what it contributes, by what method, against what ground truth or evidence; \
where the card names a detection method or ground truth, center on that. `recommended`: at most \
{RECOMMENDED_CAP} tier 2 papers, the most notable first, one sentence each on what the paper \
does. A paper marked "first posted" on a date is older work revised or newly listed today: \
describe it as an update, never as new work. `updated`: at most {UPDATED_CAP} of the papers \
listed under {UPDATED_TITLE}, the most notable first, one sentence each on what the paper \
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
paper does. A paper marked "first posted" on a date is older work revised or newly listed \
today: describe it as an update, never as new work. `updated`: at most {UPDATED_CAP} of the papers \
listed under {UPDATED_TITLE}, the most notable first, one sentence each on what the paper does. \
Use each paper's arXiv id as `id`, exactly as given, and never mention arXiv ids in \
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


# -- older papers and revisions --------------------------------------------------------------------


def first_posted(row: dict[str, Any]) -> str | None:
    """The v1 date of a paper first fetched well after it was posted, else None."""
    published, fetched = row.get("published"), row.get("fetched_at")
    if (
        isinstance(published, datetime)
        and isinstance(fetched, datetime)
        and fetched - published > OLDER_AFTER
    ):
        return published.isoformat()
    return None


def is_older(row: dict[str, Any]) -> bool:
    return bool(row.get("first_posted"))


def _day(value: str | datetime) -> str:
    day = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return f"{day:%b} {day.day}, {day.year}"


def _badge(row: dict[str, Any]) -> str:
    """` · v3, first posted May 27, 2026` for an older paper; nothing for a new one."""
    if not is_older(row):
        return ""
    version = f"v{row['version']}, " if (row.get("version") or 1) > 1 else ""
    return f" · {version}first posted {_day(row['first_posted'])}"


def render_revisions(selections: list[Selection], rows: list[dict[str, Any]], source: str) -> str:
    """Covered papers with a newer version, one dated line each; no model call."""
    names = {s.slug: s.name for s in selections}
    lines = ["**Updated since we covered them**"]
    for row in rows:
        tiers = ", ".join(
            f"tier {v['tier']} in {names[slug]}"
            for slug, v in (row.get("verdicts") or {}).items()
            if slug in names and v["tier"] <= 2 and source in (GENERAL, slug)
        )
        lines.append(
            f"- {_link(row)} · v{row['version']}, {tiers} · first covered {_day(row['covered_at'])}"
        )
    return "\n".join(lines)


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
    if mon := card.get("monitoring"):
        lines += [
            f"Monitor target: {', '.join(mon.get('target', [])) or 'none'}",
            f"Monitor type: {mon['monitor_type']}",
            f"Monitor method: {mon['monitor_method']}",
            f"Pressure tested: {mon['pressure']}",
            f"Robustness: {mon['robustness']}",
            f"Generality: {mon['generality']}",
        ]
    return lines


def _older_lines(rows: list[tuple[str, dict[str, Any]]], heading: str = "##") -> list[str]:
    """(reason, row) pairs as a capped title list for the model."""
    lines = [f"{heading} {UPDATED_TITLE} ({len(rows)} tier 2 papers)"]
    for reason, row in rows[:OLDER_INPUT_CAP]:
        lines.append(f"- {row['title']} (arXiv {row['id']}): {reason}")
    if len(rows) > OLDER_INPUT_CAP:
        lines.append(f"- and {len(rows) - OLDER_INPUT_CAP} more")
    return lines


def render_selection_input(
    selection: Selection, groups: dict[int, list[dict[str, Any]]], with_slug: bool = False
) -> str:
    """Older tier 2 papers are listed separately here, and once for all sections in general."""
    head = f"# {selection.name} (slug: {selection.slug})" if with_slug else f"# {selection.name}"
    lines = [head, f"Subject: {selection.subject}"]
    if selection.rubric:
        lines.append(f"Tier 1 rubric: {selection.rubric}")
    for row in groups[1]:
        lines += ["", f"## Tier 1: {row['title']} (arXiv {row['id']}{_badge(row)})"]
        lines.append(f"Reason: {row['verdicts'][selection.slug]['reason']}")
        lines += _card_lines(row)
    for row in groups[2]:
        if is_older(row):
            continue
        lines += ["", f"## Tier 2: {row['title']} (arXiv {row['id']})"]
        lines.append(f"Reason: {row['verdicts'][selection.slug]['reason']}")
        lines.append(f"Abstract: {(row.get('abstract') or '').strip()}")
    if with_slug:
        return "\n".join(lines)
    older = [(r["verdicts"][selection.slug]["reason"], r) for r in groups[2] if is_older(r)]
    if older:
        lines += ["", *_older_lines(older)]
    if not groups[3]:
        return "\n".join(lines)
    lines += ["", f"## Tier 3 ({len(groups[3])} papers)"]
    for row in groups[3][:TIER3_CAP]:
        reason = row["verdicts"][selection.slug]["reason"]
        lines.append(f"- {row['title']} (arXiv {row['id']}): {reason}")
    if len(groups[3]) > TIER3_CAP:
        lines.append(f"- and {len(groups[3]) - TIER3_CAP} more")
    return "\n".join(lines)


def render_general_input(selections: list[Selection], owned: Groups) -> str:
    """One block per selection that owns a tier 1 or new tier 2 paper, then older tier 2 papers."""
    blocks = [
        render_selection_input(s, owned[s.slug], with_slug=True)
        for s in selections
        if owned[s.slug][1] or any(not is_older(r) for r in owned[s.slug][2])
    ]
    older = [
        (f"{s.name}: {r['verdicts'][s.slug]['reason']}", r)
        for s in selections
        for r in owned[s.slug][2]
        if is_older(r)
    ]
    if older:
        blocks.append("\n".join(_older_lines(older, heading="#")))
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


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def render_selection_digest(
    selection: Selection, groups: dict[int, list[dict[str, Any]]], out: SelectionOutput | None
) -> str:
    """The selection's page: overview, must-read bullets, recommended bullets, the rest counted.

    A day with nothing above tier 3 says so in words rather than showing an empty template; a day
    with no members at all says that.
    """
    t1, t2, t3 = groups[1], groups[2], groups[3]
    if not (t1 or t2 or t3):
        return f"Nothing new for {selection.name} in this run: no paper passed the screen for it."
    where = f"in the Papers table, view {selection.name}"
    parts: list[str] = []
    if out is not None:
        parts.append(" ".join(out.overview.split()))
    if not (t1 or t2):
        parts.append(
            f"Nothing reached must read or recommended today; "
            f"the {_count(len(t3), 'good to know paper')} "
            f"{'is' if len(t3) == 1 else 'are'} {where}."
        )
        return "\n\n".join(parts)
    new = [r for r in t2 if not is_older(r)]
    older = [r for r in t2 if is_older(r)]
    rest, rest_older = len(new), len(older)
    if out is not None:
        if t1:
            parts.append("**Must read**\n" + "\n".join(_all_bullets(out.must_read, t1, _badge)))
        if new:
            bullets = _bullets(out.recommended[:RECOMMENDED_CAP], new)
            parts.append("**Recommended**\n" + "\n".join(bullets))
            rest = len(new) - len(bullets)
        if older:
            bullets = _bullets(out.updated[:UPDATED_CAP], older, _badge)
            if bullets:
                parts.append(f"**{UPDATED_TITLE}**\n" + "\n".join(bullets))
            rest_older = len(older) - len(bullets)
    tail = []
    if rest:
        tail.append(f"{rest} more recommended")
    if rest_older:
        tail.append(f"{_count(rest_older, 'older recommended paper')} newly updated")
    if t3:
        tail.append(f"{len(t3)} good to know")
    if tail:
        parts.append(f"Plus {_join(tail)} {where}.")
    return "\n\n".join(parts)


def _also(selections: list[Selection], row: dict[str, Any], owner: str) -> str:
    """Suffix naming the other selections where the paper is tier 1 or 2."""
    verdicts = row.get("verdicts") or {}
    others = [
        s.name
        for s in selections
        if s.slug != owner and (verdicts.get(s.slug) or {}).get("tier", 4) <= 2
    ]
    return f" (also {', '.join(others)})" if others else ""


def _shared(owned: Groups, slug: str) -> int:
    """Tier 1/2 members of a selection that the general digest lists under another one.

    An older tier 2 paper is not listed under its owner but in the updated-papers section,
    so it is not counted.
    """
    return sum(
        ((row.get("verdicts") or {}).get(slug) or {}).get("tier", 4) <= 2
        for other, tiers in owned.items()
        if other != slug
        for tier, rows in tiers.items()
        for row in rows
        if not (tier == 2 and is_older(row))
    )


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
        shared = _shared(owned, s.slug)
        if not (t1 or t2 or t3 or shared):
            continue
        sec = sections.get(s.slug)
        new = [r for r in t2 if not is_older(r)]
        older = f", {len(t2) - len(new)} older newly updated" if len(t2) > len(new) else ""
        elsewhere = f", {shared} more listed under other selections" if shared else ""
        if t1 or new:
            head = (
                f"**{s.name}** · {len(t1)} must read, {len(new)} recommended{elsewhere}{older}, "
                f"{len(t3)} good to know."
            )
        elif shared:
            head = (
                f"**{s.name}** · {shared} must read or recommended listed under other "
                f"selections{older}, {len(t3)} good to know."
            )
        else:
            head = (
                f"**{s.name}** · nothing must read or recommended{older}, {len(t3)} good to know."
            )
        if sec is not None:
            head += " " + " ".join(sec.overview.split())
        lines = [head]
        suffix = lambda r, owner=s.slug: _also(selections, r, owner) + _badge(r)  # noqa: E731
        if t1:
            lines += _all_bullets(sec.must_read if sec else [], t1, suffix)
        if new and sec is not None:
            lines += _bullets(sec.recommended[:GENERAL_RECOMMENDED_CAP], new, suffix)
        parts.append("\n".join(lines))
    owner = {r["id"]: s.name for s in selections for r in owned[s.slug][2] if is_older(r)}
    if owner:
        older = [r for s in selections for r in owned[s.slug][2] if is_older(r)]
        head = (
            f"**{UPDATED_TITLE}** · {_count(len(older), 'recommended paper')} "
            "first posted weeks or months ago."
        )
        bullets = _bullets(
            out.updated[:UPDATED_CAP] if out else [],
            older,
            lambda r: f" · {owner[r['id']]}{_badge(r)}",
        )
        lines = [head, *bullets]
        if len(older) > len(bullets):
            lines.append(f"Plus {len(older) - len(bullets)} more in the Papers table.")
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

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        effort: str = "low",
        provider_order: tuple[str, ...] = (),
    ) -> None:
        self.client, self.model, self.effort = client, model, effort
        self.provider_order = provider_order

    async def _ask(self, system: str, user: str, schema: type[BaseModel]) -> tuple[Any, Usage]:
        """Structured output the lenient way: writing models wrap JSON in fences now and then."""
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
        }
        last: Exception | None = None
        for _attempt in range(2):
            completion, usage, call_id = await recorded_completion(
                self.client,
                model=self.model,
                operation="digest",
                request={
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "response_format": response_format,
                    "extra_body": {
                        "reasoning": {"effort": self.effort},
                        "usage": {"include": True},
                        "provider": provider_preferences(self.provider_order),
                    },
                },
            )
            text = completion.choices[0].message.content or ""
            try:
                return schema.model_validate_json(unfence(text)), usage
            except ValueError as exc:
                if journal := current_journal.get():
                    await journal.reject(call_id, type(exc).__name__)
                last = exc
                log.warning("%s digest output rejected, retrying: %s", self.model, exc)
        raise RuntimeError(f"{self.model} returned no parsable digest: {last}")

    async def write_selection(
        self, selection: Selection, groups: dict[int, list[dict[str, Any]]]
    ) -> Digest:
        """A model call whenever the selection has members: a tier-3-only day gets an overview."""
        if not any(groups.values()):
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
        """General digest plus one per selection with `digest` set, members or not."""
        groups = {s.slug: by_tier(s, papers) for s in selections}
        flagged = [s for s in selections if s.digest]
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
