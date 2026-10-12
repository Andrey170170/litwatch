"""What the API returns, as types: the OpenAPI schema the web app's client is generated from."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from litwatch.core import Grade, Hand, LabelValue, ReactionReason, ReactionValue, TopicKind


class Status(BaseModel):
    """The printer's line under the masthead."""

    state: Literal["none", "running", "failed", "attention", "quiet"]
    run_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    read: int = 0
    error: str | None = None
    showing: date | None = None  # the issue the Digest falls back to after a failed run
    splits: int = 0
    ungraded: int = 0
    awaiting_triage: int = 0


class IssueSummary(BaseModel):
    id: int
    kind: Literal["daily", "weekly"]
    day: date
    lede: str | None = None


class Digest(BaseModel):
    """The feed: daily issues newest first, and the week's weekly issue pinned above them."""

    weekly: IssueSummary | None = None
    issues: list[IssueSummary]
    status: Status


class Entry(BaseModel):
    """One paper in an issue, for one Watch topic."""

    paper_id: str
    topic: str
    lead: bool  # must read: the longer take and the larger title
    take: str | None = None
    title: str
    authors: list[str]
    published: datetime | None = None
    revised: bool = False
    set_aside: bool = False  # the owner has since put it below relevant: struck through
    also_in: list[str] = Field(default_factory=list)
    saved: bool = False
    reaction: ReactionValue | None = None
    reaction_reason: ReactionReason | None = None


class TopicRef(BaseModel):
    slug: str
    name: str
    kind: TopicKind
    colour: str | None = None


class Section(BaseModel):
    topic: TopicRef
    entries: list[Entry]
    near: int = 0  # adjacent arrivals, for the fold line


class Issue(IssueSummary):
    sections: list[Section]


class Opinion(BaseModel):
    grade: Grade
    reason: str
    model: str | None = None


class Chosen(BaseModel):
    """The folded "how this was chosen" for one Watch topic."""

    topic: str
    grade: Grade
    split: bool = False
    judge: Opinion | None = None
    check: Opinion | None = None
    migrated: bool = False
    recorded_at: datetime | None = None


class LabelState(BaseModel):
    value: LabelValue
    grade: Grade | None = None
    origin: str
    provisional: bool = False


class PaperTopic(TopicRef):
    """The paper as one topic sees it."""

    grade: Grade | None = None
    take: str | None = None
    label: LabelState | None = None
    reaction: ReactionValue | None = None
    reaction_reason: ReactionReason | None = None
    member: bool = False  # in a Radar or Pulse field
    grading: bool = False


class HistoryItem(BaseModel):
    what: Literal["candidate", "graded", "missed"]
    topic: str
    at: datetime
    grade: Grade | None = None


class Paper(BaseModel):
    id: str
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    published: datetime | None = None
    updated: datetime | None = None
    added: datetime
    version: int | None = None
    revision: int = 1
    doi: str | None = None
    journal_ref: str | None = None
    arxiv_url: str
    pdf_url: str
    saved: bool = False
    hand: Hand | None = None
    topics: list[PaperTopic]
    reaction: ReactionValue | None = None  # for no topic (a plain search result)
    reaction_reason: ReactionReason | None = None
    history: list[HistoryItem]
    triage: dict[str, Any] | None = None  # topic slug -> what triage said
    chosen: list[Chosen]


class Row(BaseModel):
    """One paper in a table."""

    id: str
    title: str
    authors: list[str]
    published: datetime | None = None
    added: datetime | None = None
    grade: Grade | None = None
    grading: bool = False
    take: str | None = None
    saved: bool = False
    also_in: list[str] = Field(default_factory=list)


class Papers(BaseModel):
    rows: list[Row]
    total: int


class TopicCard(TopicRef):
    status: str
    position: int | None = None
    subject: str
    papers: int
    saved: int
    new: int
    new_saved: int
    opened_at: datetime | None = None


# -- what the owner says ---------------------------------------------------------------------------


class ReactionIn(BaseModel):
    topic: str | None = None
    value: ReactionValue
    reason: ReactionReason | None = None


class LabelIn(BaseModel):
    value: LabelValue
    grade: Grade | None = None


class SaveIn(BaseModel):
    saved: bool


class Ack(BaseModel):
    """What happened, in the words the reader sees."""

    message: str
    saved: bool
    grades: dict[str, Grade | None] = Field(default_factory=dict)  # topics whose grade moved


class LookupIn(BaseModel):
    q: str = Field(description="An arXiv id or link")


class Lookup(BaseModel):
    row: Row
    fetched: bool  # it was not in the collection until now


class MissIn(BaseModel):
    paper_id: str
    topic: str


class BibtexIn(BaseModel):
    ids: list[str]


class SessionIn(BaseModel):
    token: str
