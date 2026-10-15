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
    """Why a paper is a direct member of a class (data.md, `memberships`)."""

    CLASSIFIER = "classifier"  # at or above the threshold, or the primary class
    UNCERTAIN = "uncertain"  # the uncertain band, for grading only
    KEYWORD = "keyword"  # the keyword check, for grading only
    OWNER = "owner"  # the owner's class label, which outranks every other source
    MISS = "miss"  # a miss the owner reported


class Classified(BaseModel):
    """The classifier's answer: a probability per class and the primary class."""

    probabilities: dict[str, float]
    primary: str


class Membership(BaseModel):
    """A direct membership of class `slug`. `member` is False only for the owner's label taking
    the paper out of the class. `probability` is what brought it in, where a model did."""

    slug: str
    source: Source
    member: bool = True
    probability: float | None = None


def _focus(tree: Taxonomy) -> list[str]:
    return [t.slug for t in tree.topics if t.depth == Depth.FOCUS]


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


def keyword_hit(pattern: str, title: str, abstract: str) -> bool:
    """Whether a keyword pattern matches the title or abstract, case-insensitively."""
    return re.search(pattern, f"{title}\n{abstract}", re.IGNORECASE) is not None


def keyword(tree: Taxonomy, title: str, abstract: str) -> dict[str, str]:
    """Focus topics' classes whose keyword patterns match the title or abstract, with the first
    pattern that matched. No model is involved."""
    found: dict[str, str] = {}
    for t in tree.topics:
        if t.depth == Depth.FOCUS:
            hit = next((k for k in t.keywords if keyword_hit(k, title, abstract)), None)
            if hit:
                found[t.slug] = hit
    return found


def memberships(
    tree: Taxonomy,
    c: Classified,
    title: str,
    abstract: str,
    t: Thresholds = CHOSEN,
) -> list[Membership]:
    """Every direct membership one classification makes, each with its source: the classifier's
    (the primary class at its own probability), then the uncertain band's and the keyword
    check's for Focus topics' classes. A class reached two ways has a row per way."""
    at = thresholds(tree, t)
    found = [
        Membership(slug=s, source=Source.CLASSIFIER, probability=c.probabilities.get(s))
        for s in sorted(direct(tree, c, t))
    ]
    for f in sorted(band(tree, c, t)):
        # The highest probability in the subtree that falls in its class's band.
        p = max(x for s in tree.subtree(f) if t.floor <= (x := c.probabilities.get(s, 0.0)) < at[s])
        found.append(Membership(slug=f, source=Source.UNCERTAIN, probability=p))
    found += [
        Membership(slug=f, source=Source.KEYWORD) for f in sorted(keyword(tree, title, abstract))
    ]
    return found


def standing(rows: list[Membership]) -> list[Membership]:
    """The direct memberships that stand. Where the owner labelled a class, the label alone
    decides it: a yes is the owner's membership, a no leaves the paper out of the class whatever
    the other sources say. Elsewhere every source's membership stands."""
    owner = {m.slug: m for m in rows if m.source == Source.OWNER}
    return [m for m in rows if (m.source == Source.OWNER and m.member) or m.slug not in owner]
