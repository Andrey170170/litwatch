"""Screening: one structured model call per paper, with an optional second-stage re-screen."""

import re
from importlib import resources
from typing import Protocol

from openai import AsyncOpenAI

from litwatch.models import Confidence, Paper, ScreenOutput, ScreenResult, Usage

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PROMPT_VERSION = "v2"

# Recall safety net: a first-stage negative matching any of these is re-screened.
AUDIT_PATTERNS = [
    r"chain.of.thought",
    r"\bCoT\b",
    r"faithful",
    r"reasoning trace",
    r"monitor",
    r"hidden reasoning",
    r"steganograph",
    r"scratchpad",
    r"thinking tokens",
]
_AUDIT_RE = re.compile("|".join(AUDIT_PATTERNS), re.IGNORECASE)


def keyword_hit(paper: Paper) -> bool:
    return bool(_AUDIT_RE.search(f"{paper.title}\n{paper.abstract}"))


def load_system_prompt(version: str = PROMPT_VERSION) -> str:
    return resources.files("litwatch.prompts").joinpath(f"screen_{version}.md").read_text()


def render_user_prompt(paper: Paper) -> str:
    cats = ", ".join(paper.categories) if paper.categories else "unknown"
    return f"Title: {paper.title}\nCategories: {cats}\n\nAbstract:\n{paper.abstract}"


class Screener(Protocol):
    model: str

    async def screen(self, paper: Paper) -> tuple[ScreenOutput, Usage]: ...


class ModelScreener:
    """Screens with one OpenRouter model at a fixed reasoning effort."""

    def __init__(self, client: AsyncOpenAI, model: str, effort: str = "low") -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self._system = load_system_prompt()

    async def screen(self, paper: Paper) -> tuple[ScreenOutput, Usage]:
        completion = await self.client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": self._system},
                {"role": "user", "content": render_user_prompt(paper)},
            ],
            response_format=ScreenOutput,
            # `usage.include` makes OpenRouter report the call's cost in the usage block.
            extra_body={"reasoning": {"effort": self.effort}, "usage": {"include": True}},
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError(f"{self.model} returned no parsable output for {paper.id}")
        return parsed, usage_of(completion, self.model)


def usage_of(completion, model: str) -> Usage:
    u = completion.usage
    if u is None:
        return Usage(model=model)
    extra = getattr(u, "model_extra", None) or {}
    return Usage(
        model=model,
        prompt_tokens=u.prompt_tokens or 0,
        completion_tokens=u.completion_tokens or 0,
        cost=extra.get("cost"),
    )


class Cascade:
    """First-stage screen, then re-screen low-confidence results and audited negatives."""

    def __init__(self, primary: Screener, secondary: Screener | None) -> None:
        self.primary = primary
        self.secondary = secondary

    def needs_rescreen(self, paper: Paper, output: ScreenOutput) -> bool:
        if output.confidence == Confidence.LOW:
            return True
        return not output.is_cot_monitoring_or_faithfulness and keyword_hit(paper)

    async def screen(self, paper: Paper) -> ScreenResult:
        first, usage = await self.primary.screen(paper)
        if self.secondary is None or not self.needs_rescreen(paper, first):
            return ScreenResult(
                paper_id=paper.id,
                output=first,
                model=self.primary.model,
                prompt_version=PROMPT_VERSION,
                usage=[usage],
            )
        second, usage2 = await self.secondary.screen(paper)
        return ScreenResult(
            paper_id=paper.id,
            output=second,
            model=self.secondary.model,
            prompt_version=PROMPT_VERSION,
            rescreened=True,
            first_stage=first,
            usage=[usage, usage2],
        )


def openrouter_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
