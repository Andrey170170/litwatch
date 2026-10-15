"""What analysis says about a paper from its abstract: the Gist, Weight and facets
(docs/specs/v1/pipeline.md, sections 3 and 4)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class Weight(StrEnum):
    """How much a paper matters in its own area, whatever topic it is graded for."""

    LANDMARK = "landmark"
    SOLID = "solid"
    INCREMENTAL = "incremental"


# Each level's one definition, given word for word to every model that judges Weight (`analyse`
# now, `brief` in N3), from pipeline.md section 4.
WEIGHT_DEFINITIONS: dict[Weight, str] = {
    Weight.LANDMARK: (
        "Its area will treat it as a reference: a new model family or training recipe with "
        "released weights, a large margin on a widely used benchmark, a method shown to work "
        "across several domains, or strong evidence overturning a common assumption. Rare: "
        "about one or two papers in a hundred in an area."
    ),
    Weight.SOLID: (
        "A complete, sound contribution others in its area would build on: a method with a fair "
        "comparison, a careful analysis, a useful benchmark."
    ),
    Weight.INCREMENTAL: (
        "A small step: a minor variant, a single narrow application or evaluation, a survey or "
        "position paper without new evidence."
    ),
}


class Contribution(StrEnum):
    """What kind of contribution a paper makes. Facts, not scores."""

    METHOD = "method"
    ANALYSIS = "analysis"
    BENCHMARK = "benchmark"  # a benchmark or dataset
    THEORY = "theory"
    POSITION = "position"
    SURVEY = "survey"


class Facets(BaseModel):
    """Facts about the paper, never combined into anything."""

    contribution: Contribution
    releases_code: bool  # the abstract announces released code
    releases_weights: bool  # the abstract announces released model weights


class Analysis(BaseModel):
    """One analysis of a paper's title and abstract."""

    gist: str
    weight: Weight
    weight_reason: str
    facets: Facets
