"""Daily digest: a few paragraphs of prose about the run's tier 1 and 2 papers."""

from __future__ import annotations

from openai import AsyncOpenAI

from litwatch.models import Paper, ScreenResult, Usage
from litwatch.screen import usage_of

SYSTEM_PROMPT = """You write a short daily digest for an NLP research lab that tracks papers on \
chain-of-thought monitoring and chain-of-thought faithfulness.

You are given today's papers that passed screening, each with its tier ("must read" or "aware"), \
the screener's one-sentence reason, and the abstract. Write one to three paragraphs of plain prose \
for lab members: what came in, which papers are must-reads and why, and what themes connect them. \
Mention every paper by its exact title once. Be concrete about methods and claims; do not hype, \
do not speculate beyond the abstracts, and do not use headers, bullet points, or bold text. \
Stay under 250 words."""

EMPTY_DIGEST = "No new must-read or aware papers in this run."


def render_papers(papers: list[tuple[Paper, ScreenResult]]) -> str:
    blocks = []
    for paper, result in papers:
        blocks.append(
            f"Title: {paper.title}\n"
            f"Tier: {result.tier.value.replace('_', ' ')}\n"
            f"Reason: {result.output.reason}\n"
            f"Abstract: {paper.abstract.strip()}"
        )
    return "\n\n---\n\n".join(blocks)


async def write_digest(
    client: AsyncOpenAI,
    model: str,
    papers: list[tuple[Paper, ScreenResult]],
    effort: str = "low",
) -> tuple[str, Usage | None]:
    """Prose digest for the given papers; no model call when there is nothing to say."""
    if not papers:
        return EMPTY_DIGEST, None
    completion = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": render_papers(papers)},
        ],
        extra_body={"reasoning": {"effort": effort}, "usage": {"include": True}},
    )
    text = (completion.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError(f"{model} returned an empty digest")
    return text, usage_of(completion, model)


class Digester:
    """`write_digest` bound to a client and model, for the run pipeline."""

    def __init__(self, client: AsyncOpenAI, model: str, effort: str = "low") -> None:
        self.client, self.model, self.effort = client, model, effort

    async def write(self, papers: list[tuple[Paper, ScreenResult]]) -> tuple[str, Usage | None]:
        return await write_digest(self.client, self.model, papers, self.effort)
