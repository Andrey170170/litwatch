"""Screening: one structured call per paper judging every active selection, then a re-screen for
recall and a per-selection confirm for precision."""

from __future__ import annotations

from importlib import resources
from typing import Protocol

from openai import AsyncOpenAI
from pydantic import BaseModel, create_model

from litwatch.models import (
    Confidence,
    Paper,
    ScreenResult,
    Stage,
    StageRole,
    Usage,
    Verdict,
)
from litwatch.selection import Selection, render_selections, selections_version

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PROMPT_VERSION = "v3"
CONFIRM_TIERS = (1, 2)


def load_prompt_template(version: str = PROMPT_VERSION) -> str:
    return resources.files("litwatch.prompts").joinpath(f"screen_{version}.md").read_text()


def render_system_prompt(selections: list[Selection], version: str = PROMPT_VERSION) -> str:
    return load_prompt_template(version).replace("{selections}", render_selections(selections))


def render_user_prompt(paper: Paper) -> str:
    cats = ", ".join(paper.categories) if paper.categories else "unknown"
    return f"Title: {paper.title}\nCategories: {cats}\n\nAbstract:\n{paper.abstract}"


def output_model(selections: list[Selection]) -> type[BaseModel]:
    """One required field per selection, so the model must judge every one of them."""
    fields = {s.field: (Verdict, ...) for s in selections}
    return create_model("ScreenOutput", **fields)  # type: ignore[call-overload]


def parse_output(parsed: BaseModel, selections: list[Selection]) -> dict[str, Verdict]:
    verdicts = {}
    for s in selections:
        verdict: Verdict = getattr(parsed, s.field)
        # Without a rubric tier 1 is unreachable, whatever the model says.
        verdicts[s.slug] = verdict.model_copy(update={"tier": max(verdict.tier, s.max_tier)})
    return verdicts


class Screener(Protocol):
    model: str

    async def screen(
        self, paper: Paper, selections: list[Selection]
    ) -> tuple[dict[str, Verdict], Usage]: ...


class ModelScreener:
    """Screens with one OpenRouter model at a fixed reasoning effort."""

    def __init__(self, client: AsyncOpenAI, model: str, effort: str = "low") -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self._prompts: dict[str, tuple[str, type[BaseModel]]] = {}

    def _prepared(self, selections: list[Selection]) -> tuple[str, type[BaseModel]]:
        key = selections_version(selections)
        if key not in self._prompts:
            self._prompts[key] = (render_system_prompt(selections), output_model(selections))
        return self._prompts[key]

    async def screen(
        self, paper: Paper, selections: list[Selection]
    ) -> tuple[dict[str, Verdict], Usage]:
        system, schema = self._prepared(selections)
        completion = await self.client.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": render_user_prompt(paper)},
            ],
            response_format=schema,
            # `usage.include` makes OpenRouter report the call's cost in the usage block.
            extra_body={"reasoning": {"effort": self.effort}, "usage": {"include": True}},
        )
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError(f"{self.model} returned no parsable output for {paper.id}")
        return parse_output(parsed, selections), usage_of(completion, self.model)


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
    """Screen every selection; re-screen doubtful cases for recall; confirm tier 1 and 2 verdicts
    one selection at a time for precision."""

    def __init__(
        self,
        selections: list[Selection],
        primary: Screener,
        secondary: Screener | None = None,
        confirm: Screener | None = None,
    ) -> None:
        if not selections:
            raise ValueError("no active selections to screen against")
        self.selections = selections
        self.by_slug = {s.slug: s for s in selections}
        self.primary = primary
        self.secondary = secondary
        self.confirm = confirm

    @property
    def versions(self) -> dict[str, str]:
        return {s.slug: s.version for s in self.selections}

    def needs_rescreen(self, paper: Paper, verdicts: dict[str, Verdict]) -> bool:
        for s in self.selections:
            v = verdicts[s.slug]
            if v.confidence == Confidence.LOW:
                return True
            if v.tier == 4 and s.keyword_hit(paper):
                return True
        return False

    async def _stage(
        self, role: StageRole, screener: Screener, paper: Paper, selections: list[Selection]
    ) -> Stage:
        output, usage = await screener.screen(paper, selections)
        return Stage(role=role, model=screener.model, output=output, usage=usage)

    async def _confirm_stages(
        self, paper: Paper, verdicts: dict[str, Verdict], skip: set[str] = frozenset()
    ) -> list[Stage]:
        assert self.confirm is not None
        stages = []
        for s in self.selections:
            if s.slug in skip or verdicts[s.slug].tier not in CONFIRM_TIERS:
                continue
            stages.append(await self._stage(StageRole.CONFIRM, self.confirm, paper, [s]))
        return stages

    async def screen(self, paper: Paper) -> ScreenResult:
        stages = [await self._stage(StageRole.SCREEN, self.primary, paper, self.selections)]
        if self.secondary and self.needs_rescreen(paper, stages[-1].output):
            stages.append(
                await self._stage(StageRole.RESCREEN, self.secondary, paper, self.selections)
            )
        if self.confirm:
            stages.extend(await self._confirm_stages(paper, stages[-1].output))
        return ScreenResult(
            paper_id=paper.id,
            prompt_version=PROMPT_VERSION,
            versions=self.versions,
            stages=stages,
        )

    async def confirm_existing(self, paper: Paper, prior: ScreenResult) -> ScreenResult:
        """Append confirm stages for tier 1/2 verdicts that were never confirmed."""
        stages = await self._confirm_stages(paper, prior.verdicts, skip=prior.confirmed_slugs())
        return prior.model_copy(update={"stages": [*prior.stages, *stages]})


def openrouter_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
