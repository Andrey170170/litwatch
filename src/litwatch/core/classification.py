"""What a paper's class probabilities make it a member of (docs/specs/v1/pipeline.md, section 2).

A paper is a direct member of each class at or above its threshold and of its primary class;
an inherited member of every ancestor of those. Focus topics have two more ways in, for
grading only: the uncertain band and the keyword check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from litwatch.core.taxonomy import Depth, Taxonomy


@dataclass(frozen=True)
class Thresholds:
    """Where membership is drawn, for classes without a threshold of their own: `focus` at or
    under a Focus topic, `default` elsewhere; the uncertain band from `floor` up to the class's
    threshold. The defaults were chosen in N1 (evals/2026-10-14-classifiers.md)."""

    default: float = 0.8
    focus: float = 0.5
    floor: float = 0.1


CHOSEN = Thresholds()


class Source(StrEnum):
    """Why a paper is a direct member of a class."""

    CLASSIFIER = "classifier"
    BAND = "band"
    KEYWORD = "keyword"


class Classified(BaseModel):
    """The classifier's answer: a probability per class and the primary class."""

    probabilities: dict[str, float]
    primary: str


def _focus(tree: Taxonomy) -> list[str]:
    return [t.class_ for t in tree.topics if t.depth == Depth.FOCUS]


def thresholds(tree: Taxonomy, t: Thresholds = CHOSEN) -> dict[str, float]:
    """Each class's threshold: its own, else the Focus or the default one."""
    focus = set(_focus(tree))
    return {
        c.slug: c.threshold or (t.focus if focus & {c.slug, *tree.ancestors(c.slug)} else t.default)
        for c in tree.classes
    }


def direct(tree: Taxonomy, c: Classified, t: Thresholds = CHOSEN) -> set[str]:
    """The classes the classifier makes the paper a direct member of."""
    at = thresholds(tree, t)
    return {c.primary} | {slug for slug, p in c.probabilities.items() if p >= at[slug]}


def with_ancestors(tree: Taxonomy, slugs: set[str]) -> set[str]:
    """Direct memberships and the inherited ones they imply."""
    return slugs | {a for s in slugs for a in tree.ancestors(s)}


def band(tree: Taxonomy, c: Classified, t: Thresholds = CHOSEN) -> set[str]:
    """Focus topics' classes the paper joins by the uncertain band: some class at or under one
    has a probability in [floor, threshold)."""
    at = thresholds(tree, t)
    return {
        f
        for f in _focus(tree)
        if any(t.floor <= c.probabilities.get(s, 0.0) < at[s] for s in tree.subtree(f))
    }


def keyword(tree: Taxonomy, title: str, abstract: str) -> dict[str, str]:
    """Focus topics' classes whose keyword patterns match the title or abstract, with the first
    pattern that matched. No model is involved."""
    text = f"{title}\n{abstract}"
    found: dict[str, str] = {}
    for t in tree.topics:
        if t.depth == Depth.FOCUS:
            hit = next((k for k in t.keywords if re.search(k, text, re.IGNORECASE)), None)
            if hit:
                found[t.class_] = hit
    return found


def grading_topics(tree: Taxonomy, direct_members: set[str]) -> set[str]:
    """The topics that grade a paper: for each direct membership, the nearest followed class at
    or above it (pipeline.md, section 4). Inherited memberships add none."""
    followed = {t.class_ for t in tree.topics}
    found = set()
    for slug in direct_members:
        nearest = next((s for s in [slug, *tree.ancestors(slug)] if s in followed), None)
        if nearest:
            found.add(nearest)
    return found
