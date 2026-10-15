"""The nouns of litwatch: papers, topics, and what the pipeline and the owner say about them."""

from __future__ import annotations

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
    # always known, so it is what says which text a verdict was about.
    revision: int = Field(default=1, ge=1)
    version_history: list[dict] = Field(default_factory=list)
    doi: str | None = None
    journal_ref: str | None = None
    license: str | None = None


def keyword_patterns(value: Any) -> list[str]:
    """Keyword patterns from a list or a comma- or line-separated string, each a valid regex."""
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


class Status(StrEnum):
    """Whether a topic is in use."""

    DRAFT = "draft"  # stored, not used by any run
    ACTIVE = "active"
    PAUSED = "paused"  # history kept, no new judgements


class Grade(StrEnum):
    """A Focus topic's judgement of a paper, most inclusive first."""

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


class Opinion(BaseModel):
    """One model's grade of one paper for one Focus topic."""

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


class LabelValue(StrEnum):
    RELEVANT = "relevant"
    # A source of ideas for the topic without being about it. Not a positive for recall.
    ADJACENT = "adjacent"
    NOT_RELEVANT = "not_relevant"
    UNSURE = "unsure"  # changes nothing and is left out of every metric


class LabelOrigin(StrEnum):
    """How the labelled paper was chosen, which decides what its label can be evidence for.

    A label keeps the origin it was created with when the owner later changes its value, so a
    change of mind never moves a paper out of the sample it was drawn in.
    """

    SAMPLE = "sample"  # drawn for this topic in a Calibrate session: production precision
    CANDIDATE_SAMPLE = "candidate_sample"  # M2's random candidates; historical, never pooled
    REJECT_SAMPLE = "reject_sample"  # drawn at random from triage rejects
    LEGACY_POSITIVE = "legacy_positive"  # 0.14 placed it at tier 1 or 2
    GOLD = "gold"  # hand-labelled before v1
    READER = "reader"  # the owner's own choice: the paper page, or Calibrate off-sample
    MISS = "miss"


class Label(BaseModel):
    """The owner's word on one paper for one Focus topic. It outranks any verdict."""

    paper_id: str
    topic: str
    value: LabelValue
    grade: Grade | None = None
    origin: LabelOrigin
    # Accepted in bulk without being read; the owner's own label replaces it.
    provisional: bool = False
    note: str | None = None
    labelled_at: datetime | None = None


class Word(BaseModel):
    """The owner's word on a paper for a Focus topic, from a label or a reaction."""

    value: LabelValue
    grade: Grade | None = None


class ReactionValue(StrEnum):
    UP = "up"
    DOWN = "down"


class ReactionReason(StrEnum):
    # with a thumb down
    NOT_FOR_ME = "not_for_me"
    UNRELATED = "unrelated"  # unrelated to the topic: on a Focus topic, the word not relevant
    ALREADY_KNEW = "already_knew"
    OVERRATED = "overrated"
    # with a thumb up
    GREAT_FIND = "great_find"  # on a Focus topic, the word relevant

    @property
    def goes_with(self) -> ReactionValue:
        return ReactionValue.UP if self == ReactionReason.GREAT_FIND else ReactionValue.DOWN


class Reaction(BaseModel):
    """A thumb on a paper for the topic it was shown under, or for none. Never a label."""

    paper_id: str
    topic: str | None = None
    value: ReactionValue
    reason: ReactionReason | None = None
    reacted_at: datetime | None = None

    @field_validator("reason")
    @classmethod
    def _matches_thumb(cls, reason: ReactionReason | None, info: Any) -> ReactionReason | None:
        value = info.data.get("value")
        if reason is not None and value is not None and reason.goes_with != value:
            raise ValueError(f"{reason.value!r} does not go with a thumb {value.value}")
        return reason


class Hand(StrEnum):
    """The owner's newest use of the Save toggle."""

    SAVE = "save"
    UNSAVE = "unsave"


class Event(BaseModel):
    """A fact written to the outbox in the same transaction as the data it describes."""

    id: int | None = None
    type: str
    paper_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    # The writing transaction's id. With `id` it is the event's place in the outbox: readers
    # go by (xid, id), the order transactions began in, so none that commits late is missed.
    xid: int | None = None


class Publication(BaseModel):
    """The published record of a paper: what a BibTeX entry needs, as DBLP has it."""

    dblp_key: str | None = None  # e.g. conf/nips/VaswaniSPUJGKP17
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    kind: str = "misc"  # the BibTeX type: inproceedings, article, ...
    venue: str | None = None  # the short name: NeurIPS, TMLR
    container: str | None = None  # the proceedings' title or the journal's name
    volume: str | None = None
    number: str | None = None
    pages: str | None = None
    year: int | None = None
    doi: str | None = None
    url: str | None = None
