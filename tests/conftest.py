"""Shared fixtures: a small selection set and screening results built from verdict tiers."""

import pytest

from litwatch.models import Confidence, ScreenResult, Stage, StageRole, Usage, Verdict
from litwatch.selection import Selection

FAITH = Selection(
    slug="cot-faithfulness",
    name="CoT faithfulness",
    subject="Whether a reasoning trace omits or contradicts a computation the model performed.",
    rubric="A method or benchmark for detecting such mismatch, meant to transfer across tasks.",
    keywords=[r"chain.of.thought", r"\bCoT\b", "faithful"],
)
INTERP = Selection(
    slug="mech-interp",
    name="Mechanistic interpretability",
    subject="Understanding the internal mechanisms and representations of neural networks.",
    keywords=["sparse autoencoder", "activation patching"],
)
SELECTIONS = [FAITH, INTERP]


def verdict(tier: int, confidence: Confidence = Confidence.HIGH, reason: str = "r") -> Verdict:
    return Verdict(tier=tier, confidence=confidence, reason=reason)


def screen_result(
    paper_id: str = "1", reason: str = "Defines faithfulness & a benchmark.", **tiers: int
) -> ScreenResult:
    """A one-stage result; `tiers` maps field-style slugs (`cot_faithfulness=1`) to tiers."""
    output = {k.replace("_", "-"): verdict(t, reason=reason) for k, t in tiers.items()}
    stage = Stage(role=StageRole.SCREEN, model="m", output=output, usage=Usage(model="m"))
    return ScreenResult(
        paper_id=paper_id,
        prompt_version="v3",
        versions={s.slug: s.version for s in SELECTIONS},
        stages=[stage],
    )


@pytest.fixture
def selections() -> list[Selection]:
    return list(SELECTIONS)
