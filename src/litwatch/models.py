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
    published: str | None = None
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

    @property
    def tier(self) -> Tier:
        return derive_tier(self)


def derive_tier(flags: Flags) -> Tier:
    """Deterministic tier from the flags; the screener never picks a tier directly."""
    if flags.cot and flags.internals:
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
    confidence: Confidence
    reason: str = Field(description="One sentence")

    @property
    def flags(self) -> Flags:
        return Flags(
            cot=self.is_cot_monitoring_or_faithfulness,
            internals=self.uses_internals,
            mech_interp=self.is_mech_interp,
        )

    @property
    def tier(self) -> Tier:
        return self.flags.tier


class ScreenResult(BaseModel):
    """Final screening record for a paper, including which stage produced it."""

    paper_id: str
    output: ScreenOutput
    model: str
    prompt_version: str
    rescreened: bool = False
    first_stage: ScreenOutput | None = None

    @property
    def tier(self) -> Tier:
        return self.output.tier
