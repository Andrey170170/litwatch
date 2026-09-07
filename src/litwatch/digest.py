"""Daily digest: prose on tier 1 and 2 papers per selection, tier 3 as a code-rendered list."""

from __future__ import annotations

import re
from typing import Any

from openai import AsyncOpenAI

from litwatch.models import Usage
from litwatch.screen import usage_of
from litwatch.selection import Selection

SYSTEM_PROMPT = """You write a short daily digest for an NLP research lab's literature watch. \
The lab follows several selections, each a topic one member owns with a subject and, for some, \
a tier 1 rubric. You are given the selections and today's tier 1 (must read) and tier 2 \
(recommended) papers grouped by selection. Tier 1 papers come with a reading card written from \
the full text; tier 2 papers come with the abstract and the screener's one-sentence reason.

Write one section per selection that has papers, in the order given, as a single paragraph \
that begins with the selection's name followed by a colon. For every tier 1 paper, two to four \
sentences: what it contributes to the subject, by what method, and against what ground truth \
or evidence; where a card names a detection method or ground truth, center on that. For every \
tier 2 paper exactly one sentence. Mention every paper by its exact title once and no paper \
that is not listed. Be concrete about methods and claims; do not hype, do not speculate beyond \
what you are given, and do not use headers, bullet points, or bold text."""

EMPTY_DIGEST = "No new member papers in this run."


def _by_tier(
    selection: Selection, papers: list[dict[str, Any]], tiers: tuple[int, ...]
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {t: [] for t in tiers}
    for row in papers:
        verdict = (row.get("verdicts") or {}).get(selection.slug)
        if verdict and verdict["tier"] in grouped:
            grouped[verdict["tier"]].append(row)
    return grouped


def render_papers(selections: list[Selection], papers: list[dict[str, Any]]) -> str:
    """Tier 1 and 2 papers grouped by selection for the digest prompt. Empty when there are none."""
    blocks = []
    for s in selections:
        by_tier = _by_tier(s, papers, (1, 2))
        if not any(by_tier.values()):
            continue
        lines = [f"# {s.name}", f"Subject: {s.subject}"]
        if s.rubric:
            lines.append(f"Tier 1 rubric: {s.rubric}")
        for tier, rows in by_tier.items():
            for row in rows:
                lines.append("")
                lines.append(f"## Tier {tier}: {row['title']}")
                lines.append(f"Reason: {row['verdicts'][s.slug]['reason']}")
                card = row.get("enrichment") if tier == 1 else None
                if card:
                    lines.append(f"Key claim: {card['key_claim']}")
                    lines.append(f"Summary: {card['summary']}")
                    if faith := card.get("faithfulness"):
                        lines.append(f"Mismatch detected: {', '.join(faith['mismatch']) or 'none'}")
                        lines.append(f"Ground truth: {faith['ground_truth']}")
                        lines.append(f"Detection method: {faith['detection_method']}")
                        lines.append(f"Generality: {faith['generality']}")
                else:
                    lines.append(f"Abstract: {(row.get('abstract') or '').strip()}")
        blocks.append("\n".join(lines))
    return "\n\n---\n\n".join(blocks)


def render_tier3(selections: list[Selection], papers: list[dict[str, Any]]) -> str:
    """One paragraph per selection listing its tier 3 titles; written by code, not the model."""
    paragraphs = []
    for s in selections:
        rows = _by_tier(s, papers, (3,))[3]
        if rows:
            titles = "; ".join(" ".join(r["title"].split()) for r in rows)
            paragraphs.append(f"{s.name}, good to know: {titles}.")
    return "\n\n".join(paragraphs)


async def write_digest(
    client: AsyncOpenAI,
    model: str,
    selections: list[Selection],
    papers: list[dict[str, Any]],
    effort: str = "low",
) -> tuple[str, Usage | None]:
    """Prose digest for the given papers; the model sees tiers 1 and 2, tier 3 is a title list.

    No model call when there is nothing to write prose about.
    """
    rendered = render_papers(selections, papers)
    tail = render_tier3(selections, papers)
    if not rendered:
        return (tail or EMPTY_DIGEST), None
    completion = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": rendered},
        ],
        extra_body={"reasoning": {"effort": effort}, "usage": {"include": True}},
    )
    text = normalize(completion.choices[0].message.content or "")
    if not text:
        raise RuntimeError(f"{model} returned an empty digest")
    return "\n\n".join(t for t in (text, tail) if t), usage_of(completion, model)


def normalize(text: str) -> str:
    """One line per paragraph, paragraphs separated by a blank line."""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    return "\n\n".join(" ".join(p.split()) for p in paragraphs if p.strip())


class Digester:
    """`write_digest` bound to a client and model, for the run pipeline."""

    def __init__(self, client: AsyncOpenAI, model: str, effort: str = "low") -> None:
        self.client, self.model, self.effort = client, model, effort

    async def write(
        self, selections: list[Selection], papers: list[dict[str, Any]]
    ) -> tuple[str, Usage | None]:
        return await write_digest(self.client, self.model, selections, papers, self.effort)
