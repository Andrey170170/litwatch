"""Screening: one structured call per paper judging every active selection, then a re-screen for
recall and a per-selection confirm for precision."""

from __future__ import annotations

import asyncio
import logging
import math
from importlib import resources
from typing import Protocol

from openai import AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel, create_model

from litwatch.calls import current_journal, recorded_completion, request_key, usage_of  # noqa: F401
from litwatch.models import (
    Confidence,
    Paper,
    ScreenResult,
    Stage,
    StageRole,
    Usage,
    Verdict,
)
from litwatch.routing import provider_preferences
from litwatch.selection import Selection, render_selections

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PROMPT_VERSION = "v3"
CONFIRM_TIERS = (1, 2)
log = logging.getLogger(__name__)


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

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        effort: str = "low",
        *,
        max_completion_tokens: int | None = None,
        provider_order: tuple[str, ...] = (),
        timeout_seconds: float | None = None,
        operation: str = "screen",
    ) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.max_completion_tokens = max_completion_tokens
        self.provider_order = provider_order
        self.timeout_seconds = timeout_seconds
        if timeout_seconds is not None and (
            not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        self.operation = operation
        self._prompts: dict[tuple, tuple[str, type[BaseModel]]] = {}

    def _prepared(self, selections: list[Selection]) -> tuple[str, type[BaseModel]]:
        key = tuple((s.slug, s.version) for s in selections)
        if key not in self._prompts:
            self._prompts[key] = (render_system_prompt(selections), output_model(selections))
        return self._prompts[key]

    async def screen(
        self, paper: Paper, selections: list[Selection]
    ) -> tuple[dict[str, Verdict], Usage]:
        system, schema = self._prepared(selections)
        # Use the SDK's strict-schema conversion, as parse() does, but receive the
        # completion first: parse() can raise before exposing a billable response.
        request = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": render_user_prompt(paper)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": to_strict_json_schema(schema),
                },
            },
            # `usage.include` makes OpenRouter report the call's cost in the usage block.
            "extra_body": {
                "reasoning": {"effort": self.effort},
                "usage": {"include": True},
                "provider": provider_preferences(self.provider_order),
            },
        }
        if self.max_completion_tokens is not None:
            request["max_completion_tokens"] = self.max_completion_tokens
        key = request_key(
            {
                "validation_version": 1,
                "paper_id": paper.id,
                "operation": self.operation,
                "model": self.model,
                "request": request,
                "versions": [(s.slug, s.version) for s in selections],
            }
        )
        journal = current_journal.get()
        cached = await journal.cached(key) if journal else None
        if cached:
            parsed = schema.model_validate(cached["output"])
            original = Usage.model_validate(cached["usage"])
            usage = original.model_copy(
                update={
                    "cost": 0.0,
                    "reused": True,
                    "original_cost": original.cost,
                    "source_call_id": cached["id"],
                }
            )
            log.info("inference operation=%s reuse=%s", self.operation, usage.model_dump_json())
            return parse_output(parsed, selections), usage
        completion, usage, call_id = await recorded_completion(
            self.client,
            model=self.model,
            operation=self.operation,
            request=request,
            key=key,
            paper_id=paper.id,
            timeout_seconds=self.timeout_seconds,
        )
        try:
            choice = completion.choices[0]
            if getattr(choice, "finish_reason", None) in ("length", "content_filter"):
                raise ValueError(f"Incomplete output: {choice.finish_reason}")
            if getattr(choice.message, "refusal", None):
                raise ValueError("Model refused screening")
            parsed = schema.model_validate_json(choice.message.content or "")
            output = parse_output(parsed, selections)
        except Exception as exc:
            if journal:
                await journal.reject(call_id, type(exc).__name__)
            raise
        if journal:
            await journal.accept(call_id, parsed.model_dump(mode="json"))
        return output, usage


class Cascade:
    """Screen every selection; re-screen doubtful cases for recall; confirm tier 1 and 2 verdicts
    one selection at a time for precision."""

    def __init__(
        self,
        selections: list[Selection],
        primary: Screener,
        secondary: Screener | None = None,
        confirm: Screener | None = None,
        rescreen_ignored_keywords: tuple[str, ...] = ("benchmark",),
    ) -> None:
        if not selections:
            raise ValueError("no active selections to screen against")
        self.selections = selections
        self.by_slug = {s.slug: s for s in selections}
        self.primary = primary
        self.secondary = secondary
        self.confirm = confirm
        self.rescreen_ignored_keywords = rescreen_ignored_keywords

    @property
    def versions(self) -> dict[str, str]:
        return {s.slug: s.version for s in self.selections}

    def rescreen_selections(self, paper: Paper, verdicts: dict[str, Verdict]) -> list[Selection]:
        """Reconsider only uncertain judgments or keyword-backed rejections for each topic."""
        return [
            s
            for s in self.selections
            if verdicts[s.slug].confidence == Confidence.LOW
            or (
                verdicts[s.slug].tier == 4
                and s.keyword_hit(paper, ignored_keywords=self.rescreen_ignored_keywords)
            )
        ]

    def needs_rescreen(self, paper: Paper, verdicts: dict[str, Verdict]) -> bool:
        return bool(self.rescreen_selections(paper, verdicts))

    async def _stage(
        self, role: StageRole, screener: Screener, paper: Paper, selections: list[Selection]
    ) -> Stage:
        output, usage = await screener.screen(paper, selections)
        return Stage(role=role, model=screener.model, output=output, usage=usage)

    async def _confirm_stages(
        self, paper: Paper, verdicts: dict[str, Verdict], skip: set[str] = frozenset()
    ) -> list[Stage]:
        """One concurrent call per selection. Every call finishes before a failure is raised,
        so the journal keeps each completed stage for reuse when the paper is retried."""
        assert self.confirm is not None
        todo = [
            s
            for s in self.selections
            if s.slug not in skip and verdicts[s.slug].tier in CONFIRM_TIERS
        ]
        results = await asyncio.gather(
            *(self._stage(StageRole.CONFIRM, self.confirm, paper, [s]) for s in todo),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    async def screen(self, paper: Paper) -> ScreenResult:
        stages = [await self._stage(StageRole.SCREEN, self.primary, paper, self.selections)]
        verdicts = dict(stages[0].output)
        reconsider = self.rescreen_selections(paper, verdicts) if self.secondary else []
        if self.secondary and reconsider:
            stage = await self._stage(StageRole.RESCREEN, self.secondary, paper, reconsider)
            stages.append(stage)
            verdicts.update(stage.output)
        if self.confirm:
            stages.extend(await self._confirm_stages(paper, verdicts))
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
