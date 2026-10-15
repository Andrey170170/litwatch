"""Fit, the grade table, effective Fit and grade, and rarity (docs/specs/v1/pipeline.md, sections
4 and 5): which topics grade a paper, what a grading records, and what follows from it with the
owner's word and the paper's Weight. The tables are data, so N2c can tune them."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from litwatch.core.analysis import Weight
from litwatch.core.classification import Membership, Source, standing
from litwatch.core.taxonomy import Depth, Taxonomy
from litwatch.core.types import Grade, Status


class Fit(StrEnum):
    """How close a paper is to one topic, most inclusive first."""

    CENTRAL = "central"
    RELATED = "related"
    ADJACENT = "adjacent"
    NONE = "none"

    @property
    def rank(self) -> int:
        """0 for `central` up to 3 for `none`; a lower rank is the more inclusive Fit."""
        return list(Fit).index(self)


# Each level's one definition, given word for word to every model that judges Fit (the graders
# and `area_check`), from pipeline.md section 4.
FIT_DEFINITIONS: dict[Fit, str] = {
    Fit.CENTRAL: "The paper's main question is the topic's subject.",
    Fit.RELATED: (
        "A substantial part of the paper is about the subject, or its main result is something "
        "work on the subject would use directly, but the subject is not its main question."
    ),
    Fit.ADJACENT: (
        "The paper touches the subject (uses its methods as a tool, asks a neighbouring "
        "question, cites it as motivation) without adding to it."
    ),
    Fit.NONE: "The paper is in the topic's subtree by classification but is not about the subject.",
}

# The grade for a topic from Fit there and the paper's Weight. A starting point, tuned in N2c.
GRADES: dict[Fit, dict[Weight, Grade]] = {
    Fit.CENTRAL: {
        Weight.LANDMARK: Grade.MUST_READ,
        Weight.SOLID: Grade.MUST_READ,
        Weight.INCREMENTAL: Grade.RELEVANT,
    },
    Fit.RELATED: {
        Weight.LANDMARK: Grade.MUST_READ,
        Weight.SOLID: Grade.RELEVANT,
        Weight.INCREMENTAL: Grade.ADJACENT,
    },
    Fit.ADJACENT: dict.fromkeys(Weight, Grade.ADJACENT),
    Fit.NONE: dict.fromkeys(Weight, Grade.NONE),
}
# A paper whose analysis has not produced a Weight yet.
NO_WEIGHT = Weight.SOLID
# The owner's word of central or related never yields less than this.
OWNER_FLOOR = Grade.RELEVANT


def grade(fit: Fit, weight: Weight | None) -> Grade:
    return GRADES[fit][weight or NO_WEIGHT]


class Rarity(StrEnum):
    """What a paper's colour shows, low to high."""

    COMMON = "common"
    UNCOMMON = "uncommon"
    RARE = "rare"
    EPIC = "epic"
    LEGENDARY = "legendary"

    @property
    def level(self) -> int:
        return list(Rarity).index(self)

    def lowered(self, by: int) -> Rarity:
        return list(Rarity)[max(0, self.level - by)]


# A topic page's level for each grade; must read with Weight landmark is one higher.
TOPIC_RARITY: dict[Grade, Rarity] = {
    Grade.NONE: Rarity.COMMON,
    Grade.ADJACENT: Rarity.UNCOMMON,
    Grade.RELEVANT: Rarity.RARE,
    Grade.MUST_READ: Rarity.EPIC,
}
# How far a topic's level is lowered in a mixed list, by its depth. Tuned in N2c.
DEPTH_OFFSETS: dict[Depth, int] = {Depth.FOCUS: 0, Depth.AREA: 0, Depth.FIELD: 1}
# What a paper's Weight alone gives it in a mixed list: all an ungraded paper has.
WEIGHT_FLOORS: dict[Weight, Rarity] = {
    Weight.LANDMARK: Rarity.RARE,
    Weight.SOLID: Rarity.UNCOMMON,
    Weight.INCREMENTAL: Rarity.COMMON,
}


def topic_rarity(g: Grade, weight: Weight | None) -> Rarity:
    """The level on one topic's page, from its grade alone (and landmark for legendary)."""
    if g == Grade.MUST_READ and weight == Weight.LANDMARK:
        return Rarity.LEGENDARY
    return TOPIC_RARITY[g]


def mixed_rarity(
    levels: Iterable[tuple[str, Depth, Rarity]], weight: Weight | None
) -> tuple[Rarity, str | None]:
    """The level in a mixed list and the topic giving the highest level, from each followed
    topic's page level (topic, its depth, the level) in the owner's order: the highest of the
    levels lowered by their depth's offset, and never below the Weight's floor. The best topic is
    the first with the highest lowered level, or None for a paper no topic graded."""
    best, top = None, Rarity.COMMON
    for slug, depth, level in levels:
        lowered = level.lowered(DEPTH_OFFSETS[depth])
        if best is None or lowered.level > top.level:
            best, top = slug, lowered
    floor = WEIGHT_FLOORS[weight or NO_WEIGHT]
    return (top if top.level >= floor.level else floor), best


class Effective(BaseModel):
    """What every surface shows for a paper on one topic: the effective Fit, the grade from it
    and the current Weight, and the topic page's rarity."""

    model_config = ConfigDict(frozen=True)

    fit: Fit
    grade: Grade
    rarity: Rarity


def effective(word: Fit | None, verdict: Fit | None, weight: Weight | None) -> Effective | None:
    """The owner's word where there is one, otherwise the verdict's Fit, through the grade
    table; an owner's word of central or related never yields less than relevant."""
    fit = word or verdict
    if fit is None:
        return None
    g = grade(fit, weight)
    if word in (Fit.CENTRAL, Fit.RELATED) and not g.at_least(OWNER_FLOOR):
        g = OWNER_FLOOR
    return Effective(fit=fit, grade=g, rarity=topic_rarity(g, weight))


# -- what grading records -------------------------------------------------------------------------


class Opinion(BaseModel):
    """One grader's answer: a Fit with one sentence why, or, from `area_check`, whether the
    judge's Fit holds and the probability that it does."""

    model: str  # `provider:model@effort` of the call that produced it
    fit: Fit | None = None
    reason: str | None = None
    holds: bool | None = None
    probability: float | None = None

    @field_validator("reason")
    @classmethod
    def _no_nul(cls, value: str | None) -> str | None:
        # PostgreSQL rejects \u0000 inside jsonb; models emit it now and then.
        return value.replace("\x00", "") if value else value


class Verdict(BaseModel):
    """One grading of a paper for a topic, each its own row (`current_verdict` says which
    counts). The grade is derived from `fit` and the current Weight, never stored here."""

    id: int | None = None
    paper_id: str
    topic: str
    topic_version: str  # the topic's version hash it was graded under
    paper_revision: int = 1
    fit: Fit
    reason: str
    opinions: dict[str, Opinion]  # by role, in the order asked
    split: bool = False  # two graders gave different Fits
    escalated: bool = False  # an Area grading `check` was asked to grade too
    recorded_at: datetime | None = None


def current_verdict(verdicts: Iterable[Verdict]) -> Verdict | None:
    """The verdict that counts for one paper and topic: the newest paper revision that has one,
    then the most recently recorded. A verdict stays valid on its topic whatever version of the
    topic it was made under (pipeline.md, section 10); an older revision's stands until the new
    text is graded."""
    return max(verdicts, key=lambda v: (v.paper_revision, v.recorded_at, v.id or 0), default=None)


# -- which topics grade a paper -------------------------------------------------------------------


def followed(tree: Taxonomy) -> dict[str, Depth]:
    """The followed topics and their depths. A draft topic is stored, not used by any run."""
    return {t.slug: t.depth for t in tree.topics if t.status != Status.DRAFT}


def grading_topics(
    tree: Taxonomy, memberships: Iterable[Membership], held: Iterable[str] = ()
) -> set[str]:
    """The topics that grade a paper (pipeline.md, sections 4 and 8): for each of its standing
    direct memberships, the nearest followed class at or above it; a membership from the
    uncertain band or the keyword check counts for its Focus topic only. `held` are the topics
    whose verdict on an earlier revision of the paper held a grade above none: they grade the new
    text even outside their subtree. A class no longer in the tree leads nowhere."""
    topics = followed(tree)
    classes = {c.slug for c in tree.classes}
    found = {s for s in held if s in topics}
    for m in standing(list(memberships)):
        if m.slug not in classes:
            continue
        if m.source in (Source.UNCERTAIN, Source.KEYWORD):
            if topics.get(m.slug) == Depth.FOCUS:
                found.add(m.slug)
            continue
        nearest = next((s for s in [m.slug, *tree.ancestors(m.slug)] if s in topics), None)
        if nearest:
            found.add(nearest)
    return found
