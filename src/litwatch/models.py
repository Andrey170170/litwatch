"""Core data types: papers, screening output, tier derivation."""

from enum import StrEnum

from pydantic import BaseModel, Field


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


class Tier(StrEnum):
    MUST_READ = "must_read"
    AWARE = "aware"
    MECH_INTERP = "mech_interp"
    NONE = "none"


class Flags(BaseModel):
    """The three topic flags. Labels in the eval set and screener output share this shape."""

    cot: bool = Field(description="About CoT monitoring, faithfulness, or hidden reasoning")
    internals: bool = Field(description="Relies on model internals")
    mech_interp: bool = Field(description="Mechanistic interpretability work")
    foundational: bool = Field(
        default=False,
        description="Proposes a definition, ground truth, benchmark, or metric meta-evaluation",
    )

    @property
    def tier(self) -> Tier:
        return derive_tier(self)


def derive_tier(flags: Flags) -> Tier:
    """Deterministic tier from the flags; the screener never picks a tier directly.

    On-topic papers are must-read when they use internals or when they are foundational:
    a definition, ground truth, or benchmark is required reading whatever its method.
    """
    if flags.cot and (flags.internals or flags.foundational):
        return Tier.MUST_READ
    if flags.cot:
        return Tier.AWARE
    if flags.mech_interp:
        return Tier.MECH_INTERP
    return Tier.NONE


class ScreenOutput(BaseModel):
    """Structured model output. Field names are part of the prompt contract."""

    is_cot_monitoring_or_faithfulness: bool
    uses_internals: bool
    is_mech_interp: bool
    proposes_definition_or_benchmark: bool
    confidence: Confidence
    reason: str = Field(description="One sentence")

    @property
    def flags(self) -> Flags:
        return Flags(
            cot=self.is_cot_monitoring_or_faithfulness,
            internals=self.uses_internals,
            mech_interp=self.is_mech_interp,
            foundational=self.proposes_definition_or_benchmark,
        )

    @property
    def tier(self) -> Tier:
        return self.flags.tier


class Usage(BaseModel):
    """One model call's accounting. `cost` is in USD as reported by the provider."""

    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None


class StageRole(StrEnum):
    """Cascade stages in order. Each later stage's output replaces the earlier verdict."""

    SCREEN = "screen"  # every paper, cheap model
    RESCREEN = "rescreen"  # low confidence or keyword-audit negatives: the recall step
    CONFIRM = "confirm"  # tier 1 and 2 positives, different model family: the precision step


STAGE_ORDER = [StageRole.SCREEN, StageRole.RESCREEN, StageRole.CONFIRM]


class Stage(BaseModel):
    role: StageRole
    model: str
    output: ScreenOutput
    usage: Usage


class ScreenResult(BaseModel):
    """Screening record for a paper: every stage that ran, last one wins."""

    paper_id: str
    prompt_version: str
    stages: list[Stage] = Field(min_length=1)

    @property
    def output(self) -> ScreenOutput:
        return self.stages[-1].output

    @property
    def model(self) -> str:
        return self.stages[-1].model

    @property
    def tier(self) -> Tier:
        return self.output.tier

    def output_after(self, role: StageRole) -> ScreenOutput:
        """The verdict as it stood once stages up to `role` had run."""
        cutoff = STAGE_ORDER.index(role)
        eligible = [s for s in self.stages if STAGE_ORDER.index(s.role) <= cutoff]
        return eligible[-1].output

    def has(self, role: StageRole) -> bool:
        return any(s.role == role for s in self.stages)

    @property
    def rescreened(self) -> bool:
        return self.has(StageRole.RESCREEN)

    @property
    def confirmed(self) -> bool:
        return self.has(StageRole.CONFIRM)

    @property
    def usage(self) -> list[Usage]:
        return [s.usage for s in self.stages]

    @property
    def cost(self) -> float:
        return sum(s.usage.cost or 0.0 for s in self.stages)
