"""Daily digest: prose per selection about the run's member papers, must-reads from their cards."""

from __future__ import annotations

import re
from typing import Any

from openai import AsyncOpenAI

from litwatch.models import Usage
from litwatch.screen import usage_of
from litwatch.selection import Selection

SYSTEM_PROMPT = """You write a short daily digest for an NLP research lab's literature watch. \
The lab follows several selections, each a topic one member owns with a subject and, for some, \
a tier 1 rubric. You are given the selections and today's member papers grouped by selection \
and tier: tier 1 (must read), tier 2 (recommended), tier 3 (good to know). Tier 1 papers come \
with a reading card written from the full text; the others come with the abstract and the \
screener's one-sentence reason.

Write one section per selection that has papers, in the order given. Start each section with \
a paragraph that begins with the selection's name followed by a colon. For every tier 1 paper, \
two to four sentences: what it contributes to the subject, by what method, and against what \
ground truth or evidence; where a card names a detection method or ground truth, center on \
that. For tier 2 papers one sentence each. For tier 3, one sentence listing the titles. \
Mention every paper by its exact title once. Be concrete about methods and claims; do not \
hype, do not speculate beyond what you are given, and do not use headers, bullet points, or \
bold text. Stay under 180 words per selection."""

EMPTY_DIGEST = "No new member papers in this run."


def render_papers(selections: list[Selection], papers: list[dict[str, Any]]) -> str:
    """Group member papers by selection and tier for the digest prompt. Empty when no members."""
    blocks = []
    for s in selections:
        by_tier: dict[int, list[dict[str, Any]]] = {1: [], 2: [], 3: []}
        for row in papers:
            verdict = (row.get("verdicts") or {}).get(s.slug)
            if verdict and verdict["tier"] in by_tier:
                by_tier[verdict["tier"]].append(row)
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


async def write_digest(
    client: AsyncOpenAI,
    model: str,
    selections: list[Selection],
    papers: list[dict[str, Any]],
    effort: str = "low",
) -> tuple[str, Usage | None]:
    """Prose digest for the given papers; no model call when there is nothing to say."""
    rendered = render_papers(selections, papers)
    if not rendered:
        return EMPTY_DIGEST, None
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
    return text, usage_of(completion, model)


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
