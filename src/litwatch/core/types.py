"""The nouns of litwatch: papers, topics, and what the pipeline and the owner say about them."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class Paper(BaseModel):
    """Paper metadata as stored and judged. `id` is the version-stripped arXiv id."""

    id: str
    title: str
    abstract: str
    authors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    primary_category: str | None = None
    published: str | None = None
    updated: str | None = None
    url: str | None = None
    # Only explicit upstream version identifiers; absent does not mean v1.
    version: int | None = Field(default=None, ge=1)
    # Counts rewrites of the title or abstract, set by the store. Unlike `version` it is
    # always known, so it is what says which text a candidate or verdict was about.
    revision: int = Field(default=1, ge=1)
    version_history: list[dict] = Field(default_factory=list)
    doi: str | None = None
    journal_ref: str | None = None
    license: str | None = None


class TopicKind(StrEnum):
    """The service a topic gets: graded per paper, reviewed weekly, or summarised weekly."""

    WATCH = "watch"
    RADAR = "radar"
    PULSE = "pulse"


class Status(StrEnum):
    DRAFT = "draft"  # stored, not used by any run
    ACTIVE = "active"
    PAUSED = "paused"  # history kept, no new judgements


class Grade(StrEnum):
    """A Watch topic's judgement of a paper, most inclusive first."""

    MUST_READ = "must_read"
    RELEVANT = "relevant"
    ADJACENT = "adjacent"
    NONE = "none"

    @property
    def rank(self) -> int:
        """0 for `must_read` up to 3 for `none`; a lower rank is the more inclusive grade."""
        return list(Grade).index(self)

    def at_least(self, other: Grade) -> bool:
        return self.rank <= other.rank


class Topic(BaseModel):
    """Something the owner follows. `revision` is set by the store when the topic is saved."""

    slug: str = Field(pattern=SLUG_RE.pattern)
    name: str = Field(min_length=2, max_length=80)
    kind: TopicKind
    subject: str = Field(min_length=20)
    rubric: str | None = None  # Watch: what makes a paper `must_read`
    interests: str | None = None  # Radar: what the owner cares about within the field
    keywords: list[str] = Field(default_factory=list)  # Watch: regexes for the keyword audit
    status: Status = Status.ACTIVE
    revision: int | None = None

    @field_validator("slug")
    @classmethod
    def _usable_as_a_field(cls, value: str) -> str:
        # The slug names a field of the triage reply, and pydantic keeps some names for itself
        # (`model-dump`, `copy`). Such a topic would stop triage for every paper.
        if hasattr(BaseModel, value.replace("-", "_")):
            raise ValueError(f"{value!r} is a reserved name; choose another slug")
        return value

    @field_validator("rubric", "interests", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @field_validator("subject", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("keywords", mode="before")
    @classmethod
    def _keywords(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = re.split(r"[,\n]", value)
        items = [str(k).strip() for k in value if str(k).strip()]
        for pattern in items:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"keyword {pattern!r} is not a valid regex: {exc}") from exc
        return items

    @property
    def field(self) -> str:
        """The slug as a structured-output field name."""
        return self.slug.replace("-", "_")

    @property
    def version(self) -> str:
        """SHA-256 of what a model sees of the topic. Name, status and revision do not count.

        It says whether two versions differ, not which is newer; `revision` orders them.
        """
        content = {
            "kind": self.kind.value,
            "subject": self.subject,
            "rubric": self.rubric,
            "interests": self.interests,
            "keywords": sorted(self.keywords),
        }
        canonical = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class Opinion(BaseModel):
    """One model's grade of one paper for one Watch topic."""

    grade: Grade
    reason: str
    model: str | None = None  # `provider:model@effort` of the call that produced it

    @field_validator("reason")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        # PostgreSQL rejects \u0000 inside jsonb; models emit it now and then.
        return value.replace("\x00", "")


class Verdict(BaseModel):
    """A recorded grading. Each grading is its own row; see `current_verdict` for which counts."""

    id: int | None = None
    paper_id: str
    topic: str
    topic_revision: int
    paper_version: int | None = None
    paper_revision: int = 1
    grade: Grade
    split: bool = False
    judge: Opinion | None = None
    check: Opinion | None = None
    migrated: bool = False  # carried over from a 0.x tier, with a single opinion
    recorded_at: datetime | None = None


class Membership(BaseModel):
    """Triage placed the paper in a Radar or Pulse topic's field."""

    paper_id: str
    topic: str
    topic_revision: int
    paper_version: int | None = None


class CandidateState(StrEnum):
    PENDING = "pending"
    GRADED = "graded"


class Candidate(BaseModel):
    """A paper that triage or the keyword audit put forward for a Watch topic."""

    paper_id: str
    topic: str
    topic_revision: int
    paper_version: int | None = None
    paper_revision: int = 1
    by_triage: bool = False
    by_keyword: bool = False
    reason: str | None = None
    state: CandidateState = CandidateState.PENDING
    failed_runs: int = 0
    last_error: str | None = None


class LabelValue(StrEnum):
    RELEVANT = "relevant"
    # A source of ideas for the topic without being about it. Not a positive for recall.
    ADJACENT = "adjacent"
    NOT_RELEVANT = "not_relevant"
    UNSURE = "unsure"  # changes nothing and is left out of every metric


class LabelOrigin(StrEnum):
    """How the labelled paper was chosen, which decides what its label can be evidence for."""

    CANDIDATE_SAMPLE = "candidate_sample"  # drawn at random from candidates
    REJECT_SAMPLE = "reject_sample"  # drawn at random from triage rejects
    LEGACY_POSITIVE = "legacy_positive"  # 0.14 placed it at tier 1 or 2
    GOLD = "gold"  # hand-labelled before v1
    INBOX = "inbox"
    MISS = "miss"


class Label(BaseModel):
    """The owner's word on one paper for one Watch topic. It outranks any verdict."""

    paper_id: str
    topic: str
    value: LabelValue
    grade: Grade | None = None
    origin: LabelOrigin
    # Accepted in bulk without being read; the owner's own label replaces it.
    provisional: bool = False
    note: str | None = None
    labelled_at: datetime | None = None


class Event(BaseModel):
    """A fact written to the outbox in the same transaction as the data it describes."""

    id: int | None = None
    type: str
    paper_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
