"""The taxonomy: the class tree every paper is sorted into, and the topics the owner follows
(docs/specs/v1/taxonomy.md). A topic is a followed class; its subject is the class's description.

Classes and topics are versioned (docs/specs/v1/pipeline.md, section 10): `version` is a hash of
what a model reads of one, and the store raises its revision only when the hash changes."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from litwatch.core.types import SLUG_RE, Status, keyword_patterns

MAX_LEVELS = 3


def _digest(content: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON. It says whether two versions differ, not which is newer."""
    canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class Depth(StrEnum):
    """How closely the owner follows a topic: the research question, the area, the wider field."""

    FOCUS = "focus"
    AREA = "area"
    FIELD = "field"


class TaxonomyClass(BaseModel):
    """One node of the tree. `parent` is the parent's slug, None at the top."""

    model_config = ConfigDict(extra="forbid")  # a misspelled field must not pass silently

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

    @property
    def version(self) -> str:
        """The hash of name, description, parent and threshold: what the classifier reads."""
        return _digest(
            {
                "name": self.name,
                "description": self.description,
                "parent": self.parent,
                "threshold": self.threshold,
            }
        )


class Topic(BaseModel):
    """A class the owner follows, keyed by the class's slug (`class` in YAML and JSON). The
    graders read its subject (the Fit definition) and, for a Focus topic, its rubric (what each
    Fit level means for it)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    slug: str = Field(alias="class")
    depth: Depth
    monogram: str = Field(min_length=1, max_length=2)
    colour: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    status: Status = Status.ACTIVE
    subject: str = Field(min_length=20)
    rubric: str | None = None
    interests: str | None = None  # what the owner cares about most within it
    keywords: list[str] = Field(default_factory=list)  # regexes for the Focus keyword check

    @field_validator("subject", "rubric", "interests", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("keywords", mode="before")
    @classmethod
    def _keywords(cls, value: Any) -> list[str]:
        return keyword_patterns(value)

    @property
    def version(self) -> str:
        """The hash of depth, subject, rubric, interests and keywords (in any order): what the
        graders read. Badge, status and place do not count."""
        return _digest(
            {
                "depth": self.depth.value,
                "subject": self.subject,
                "rubric": self.rubric,
                "interests": self.interests,
                "keywords": sorted(self.keywords),
            }
        )


class Taxonomy(BaseModel):
    """The tree, parents before children and siblings in the owner's order, and the followed
    topics in the owner's order. Construction fails with every problem found."""

    model_config = ConfigDict(extra="forbid")

    classes: list[TaxonomyClass] = Field(min_length=1)
    topics: list[Topic] = Field(default_factory=list)

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
        followed = Counter(t.slug for t in self.topics)
        found += [f"class {s!r} is followed {n} times" for s, n in followed.items() if n > 1]
        for t in self.topics:
            if t.slug not in slugs:
                found.append(f"topic on {t.slug!r}: no such class")
            elif self.get(t.slug).description != " ".join(t.subject.split()):
                found.append(f"class {t.slug!r} is followed; its description must be its subject")
            if t.depth == Depth.FOCUS and t.rubric is None:
                found.append(f"Focus topic {t.slug!r} has no rubric")
            if t.depth != Depth.FOCUS and t.rubric is not None:
                found.append(f"{t.depth.value} topic {t.slug!r} has a rubric; only Focus topics do")
        return found

    @property
    def classes_version(self) -> str:
        """The taxonomy revision a classification records: the hash of every class's version in
        tree order, which is what the classifier's request is built from. Topics do not count."""
        return _digest({"classes": [[c.slug, c.version] for c in self.classes]})

    def get(self, slug: str) -> TaxonomyClass:
        return next(c for c in self.classes if c.slug == slug)

    def level(self, slug: str) -> int:
        """1 for a top-level class."""
        parent = self.get(slug).parent
        return 1 if parent is None else 1 + self.level(parent)

    def ancestors(self, slug: str) -> list[str]:
        """The classes above `slug`, nearest first."""
        found, parent = [], self.get(slug).parent
        while parent is not None:
            found.append(parent)
            parent = self.get(parent).parent
        return found

    def subtree(self, slug: str) -> list[str]:
        """`slug` and every class under it."""
        return [c.slug for c in self.classes if c.slug == slug or slug in self.ancestors(c.slug)]

    def children(self, slug: str | None) -> list[TaxonomyClass]:
        """The classes directly under `slug`, or the top-level classes for None."""
        return [c for c in self.classes if c.parent == slug]

    def topic(self, slug: str) -> Topic | None:
        return next((t for t in self.topics if t.slug == slug), None)

    def shown(self) -> list[Topic]:
        """The topics in use (drafts left out) in the one order every list keeps: Focus, Area,
        Field, each in the owner's order."""
        depths = list(Depth)
        used = [t for t in self.topics if t.status != Status.DRAFT]
        return sorted(used, key=lambda t: depths.index(t.depth))

    def under(self, slug: str) -> list[Topic]:
        """The shown topics at or under class `slug`, in that order."""
        below = set(self.subtree(slug))
        return [t for t in self.shown() if t.slug in below]

    def following(self, topic: Topic) -> Taxonomy:
        """This taxonomy with `topic` in place of its class's topic, or last if the class was not
        followed, and the class described by its subject. Fails as construction does."""
        classes = [
            TaxonomyClass.model_validate(c.model_dump() | {"description": topic.subject})
            if c.slug == topic.slug
            else c
            for c in self.classes
        ]
        topics = [t for t in self.topics if t.slug != topic.slug]
        at = next((i for i, t in enumerate(self.topics) if t.slug == topic.slug), len(topics))
        topics.insert(at, topic)
        return Taxonomy(classes=classes, topics=topics)
