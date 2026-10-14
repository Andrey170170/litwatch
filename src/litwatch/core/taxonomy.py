"""The taxonomy: the class tree every paper is sorted into, and the topics the owner follows
(docs/specs/v1/taxonomy.md). A topic is a followed class; its subject is the class's description."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from litwatch.core.types import SLUG_RE, keyword_patterns

MAX_LEVELS = 3


class Depth(StrEnum):
    """How closely the owner follows a topic: the research question, the area, the wider field."""

    FOCUS = "focus"
    AREA = "area"
    FIELD = "field"


class TaxonomyClass(BaseModel):
    """One node of the tree. `parent` is the parent's slug, None at the top."""

    slug: str = Field(pattern=SLUG_RE.pattern)
    name: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=10)
    parent: str | None = None
    # A paper is a direct member at or above this probability; None takes the configured default.
    threshold: float | None = Field(default=None, gt=0, lt=1)

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return " ".join(value.split()) if isinstance(value, str) else value


class FollowedTopic(BaseModel):
    """A class the owner follows. The graders read its subject (the Fit definition) and, for a
    Focus topic, its rubric (what each Fit level means for it)."""

    model_config = ConfigDict(populate_by_name=True)

    class_: str = Field(alias="class")
    depth: Depth
    subject: str = Field(min_length=20)
    rubric: str | None = None
    keywords: list[str] = Field(default_factory=list)  # regexes for the Focus keyword check
    monogram: str = Field(min_length=1, max_length=2)
    colour: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("subject", "rubric", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("keywords", mode="before")
    @classmethod
    def _keywords(cls, value: Any) -> list[str]:
        return keyword_patterns(value)


class Taxonomy(BaseModel):
    """The tree, parents before children and siblings in the owner's order, and the followed
    topics in the owner's order. Construction fails with every problem found."""

    classes: list[TaxonomyClass]
    topics: list[FollowedTopic] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid(self) -> Taxonomy:
        if problems := self.problems():
            raise ValueError("; ".join(problems))
        return self

    def problems(self) -> list[str]:
        found: list[str] = []
        slugs = Counter(c.slug for c in self.classes)
        names = Counter(c.name.casefold() for c in self.classes)
        found += [f"class {s!r} appears {n} times" for s, n in slugs.items() if n > 1]
        found += [f"the name {s!r} is used {n} times" for s, n in names.items() if n > 1]
        seen: set[str] = set()
        for c in self.classes:
            if c.parent is not None and c.parent not in seen:
                found.append(f"class {c.slug!r} comes before its parent {c.parent!r} or has none")
            seen.add(c.slug)
        if not found:
            found += [
                f"class {c.slug!r} is more than {MAX_LEVELS} levels deep"
                for c in self.classes
                if self.level(c.slug) > MAX_LEVELS
            ]
        followed = Counter(t.class_ for t in self.topics)
        found += [f"class {s!r} is followed {n} times" for s, n in followed.items() if n > 1]
        for t in self.topics:
            if t.class_ not in slugs:
                found.append(f"topic on {t.class_!r}: no such class")
            if t.depth == Depth.FOCUS and t.rubric is None:
                found.append(f"Focus topic {t.class_!r} has no rubric")
            if t.depth != Depth.FOCUS and t.rubric is not None:
                found.append(
                    f"{t.depth.value} topic {t.class_!r} has a rubric; only Focus topics do"
                )
        return found

    def get(self, slug: str) -> TaxonomyClass:
        return next(c for c in self.classes if c.slug == slug)

    def level(self, slug: str) -> int:
        """1 for a top-level class."""
        parent = self.get(slug).parent
        return 1 if parent is None else 1 + self.level(parent)

    def children(self, slug: str | None) -> list[TaxonomyClass]:
        """The classes directly under `slug`, or the top-level classes for None."""
        return [c for c in self.classes if c.parent == slug]

    def topic(self, slug: str) -> FollowedTopic | None:
        return next((t for t in self.topics if t.class_ == slug), None)
