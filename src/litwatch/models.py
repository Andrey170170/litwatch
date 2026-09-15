"""Core data types: papers, per-selection verdicts, screening results."""

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Paper(BaseModel):
    """Paper metadata as stored and screened. `id` is the version-stripped arXiv id."""

    id: str
    title: str
    abstract: str
    authors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    primary_category: str | None = None
    published: str | None = None
    updated: str | None = None
    url: str | None = None


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Tiers are per selection, one scale for all of them:
#   1 must read, 2 recommended, 3 good to know, 4 not a member (stored as absence in lists).
TIERS = (1, 2, 3, 4)
NOT_MEMBER = 4
ROUTED_TIERS = (1, 2)  # Zotero item; tier 1 also gets the PDF and deep processing
VISIBLE_TIERS = (1, 2, 3)  # what the lab sees in Notion


class Verdict(BaseModel):
    """One selection's judgement of one paper. Field names are part of the prompt contract."""

    tier: int = Field(ge=1, le=4)
    confidence: Confidence
    reason: str = Field(description="One sentence")

    @field_validator("reason")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        # PostgreSQL rejects \u0000 inside jsonb; models emit it now and then.
        return value.replace("\x00", "")


def best_tier(verdicts: dict[str, Verdict]) -> int:
    return min((v.tier for v in verdicts.values()), default=NOT_MEMBER)


class Usage(BaseModel):
    """One model call's accounting. `cost` is in USD as reported by the provider."""

    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None
    generation_id: str | None = None
    provider: str | None = None
    service_tier: str | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_discount: float | None = None
    # A persisted response retains its original accounting; reuse charges this run zero.
    reused: bool = False
    original_cost: float | None = None
    source_call_id: int | None = None


class StageRole(StrEnum):
    """Cascade stages in order. A later stage's verdict replaces the earlier one per selection."""

    SCREEN = "screen"  # every paper, every selection, cheap model
    RESCREEN = "rescreen"  # low confidence or keyword-audit negatives: the recall step
    CONFIRM = "confirm"  # tier 1 and 2 verdicts, one selection at a time: the precision step


STAGE_ORDER = [StageRole.SCREEN, StageRole.RESCREEN, StageRole.CONFIRM]


class Stage(BaseModel):
    role: StageRole
    model: str
    output: dict[str, Verdict]  # selection slug -> verdict; a confirm stage carries one slug
    usage: Usage


class ScreenResult(BaseModel):
    """Screening record for a paper: every stage that ran, later stages win per selection."""

    schema_version: int = 2
    paper_id: str
    prompt_version: str
    versions: dict[str, str]  # selection slug -> spec version the verdict was judged under
    stages: list[Stage] = Field(min_length=1)

    @property
    def verdicts(self) -> dict[str, Verdict]:
        return self.verdicts_after(StageRole.CONFIRM)

    def verdicts_after(self, role: StageRole) -> dict[str, Verdict]:
        """The verdicts as they stood once stages up to `role` had run."""
        cutoff = STAGE_ORDER.index(role)
        merged: dict[str, Verdict] = {}
        for stage in self.stages:
            if STAGE_ORDER.index(stage.role) <= cutoff:
                merged.update(stage.output)
        return merged

    @property
    def best_tier(self) -> int:
        return best_tier(self.verdicts)

    @property
    def best(self) -> tuple[str, Verdict] | None:
        """The selection that places the paper highest; None when it is a member of none."""
        members = [(slug, v) for slug, v in self.verdicts.items() if v.tier < NOT_MEMBER]
        return min(members, key=lambda kv: kv[1].tier) if members else None

    def tier_for(self, slug: str) -> int:
        verdict = self.verdicts.get(slug)
        return verdict.tier if verdict else NOT_MEMBER

    def members(self, max_tier: int = 3) -> list[str]:
        return [slug for slug, v in self.verdicts.items() if v.tier <= max_tier]

    @property
    def model(self) -> str:
        return self.stages[-1].model

    def has(self, role: StageRole) -> bool:
        return any(s.role == role for s in self.stages)

    @property
    def rescreened(self) -> bool:
        return self.has(StageRole.RESCREEN)

    @property
    def confirmed(self) -> bool:
        return self.has(StageRole.CONFIRM)

    def confirmed_slugs(self) -> set[str]:
        return {slug for s in self.stages if s.role == StageRole.CONFIRM for slug in s.output}

    @property
    def usage(self) -> list[Usage]:
        return [s.usage for s in self.stages]

    @property
    def cost(self) -> float:
        return sum(s.usage.cost or 0.0 for s in self.stages)
