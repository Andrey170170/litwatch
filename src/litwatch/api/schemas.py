"""What the API returns, as types: the OpenAPI schema the web app's client is generated from."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from litwatch.core import (
    Event,
    Grade,
    Hand,
    LabelValue,
    Publication,
    ReactionReason,
    ReactionValue,
    TopicKind,
)


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
    reviews_failed: int = 0


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


class Pick(BaseModel):
    """A paper a review picked, with its sentence on why."""

    paper_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    published: datetime | None = None
    why: str
    saved: bool = False
    reaction: ReactionValue | None = None  # the owner's thumb, for the review's topic
    reaction_reason: ReactionReason | None = None


class Review(BaseModel):
    """A Radar or Pulse topic's review of a day or a week. A failed one says so in one line;
    a quiet one had no papers to read."""

    period: Literal["day", "week"]
    day: date
    state: Literal["written", "failed", "quiet"]
    text: str | None = None
    picks: list[Pick] = Field(default_factory=list)
    members: int = 0
    will_retry: bool = False  # failed, and the next scheduled run tries it again


class Listed(BaseModel):
    """A paper named in a recap, with the owner's thumb on it for the recap's topic."""

    paper_id: str
    title: str
    reaction: ReactionValue | None = None
    reaction_reason: ReactionReason | None = None


class Recap(BaseModel):
    """A Watch topic's week: prose over its entries, then the entries as one-liners, what
    the owner set aside and the misses reported."""

    state: Literal["written", "failed", "quiet"]
    text: str | None = None
    papers: list[Listed] = Field(default_factory=list)
    set_aside: list[Listed] = Field(default_factory=list)
    misses: list[Listed] = Field(default_factory=list)
    will_retry: bool = False  # failed, and the next scheduled run tries it again


class Section(BaseModel):
    topic: TopicRef
    entries: list[Entry]
    near: int = 0  # adjacent arrivals, for the fold line
    review: Review | None = None  # a Radar or Pulse topic's section
    recap: Recap | None = None  # a Watch topic's section in a weekly issue


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


class DeepCard(BaseModel):
    """A deep card: the reading card the deep service wrote, as it wrote it."""

    card: dict[str, Any]
    model: str | None = None
    written_at: datetime | None = None


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
    versions: list[dict[str, Any]] = Field(default_factory=list)  # arXiv versions seen
    triage: dict[str, Any] | None = None  # topic slug -> what triage said
    chosen: list[Chosen]
    publication: Publication | None = None  # the published version, when one is known
    publication_by_owner: bool = False  # the owner chose it (or chose "none")
    card: DeepCard | None = None  # shown whenever one is stored, integration or not
    card_requestable: bool = False  # an integration that answers card requests is loaded


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
    take_topic: str | None = None  # whose take it is
    saved: bool = False
    also_in: list[str] = Field(default_factory=list)  # other topics it is relevant to
    source: str = "collection"  # or the search provider that found it outside the collection
    score: float | None = None  # the provider's, for its "best match" order


class Papers(BaseModel):
    """A page of the table. A search also brings the papers search providers found that the
    collection lacks, as a second band under the first; a provider that failed or was too
    slow is named in `left_out`."""

    rows: list[Row]
    total: int
    outside: list[Row] = Field(default_factory=list)
    left_out: list[str] = Field(default_factory=list)


class TopicCard(TopicRef):
    status: str
    cadence: Literal["weekly", "daily"] = "weekly"
    last_review: date | None = None
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


class CitedIn(BaseModel):
    """A paper outside the collection, as a search hit gives it: enough for an arXiv entry."""

    id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None


class BibtexFilter(BaseModel):
    """A papers-table filter, as `GET /api/papers` takes it."""

    q: str | None = None
    topic: str | None = None
    adjacent: bool = False
    saved: bool | None = None
    year: int | None = None
    source: Literal["collection", "outside"] | None = None  # one band only, as the table


class BibtexIn(BaseModel):
    ids: list[str] = Field(default_factory=list)  # papers in the collection
    filter: BibtexFilter | None = None  # every collection paper a table filter matches, too
    cited: list[CitedIn] = Field(default_factory=list)  # provider hits the collection lacks


class PublicationIn(BaseModel):
    """The owner's correction: a DBLP key, a DOI, or that there is no published version."""

    dblp_key: str | None = None
    doi: str | None = None
    none: bool = False

    @model_validator(mode="after")
    def _one(self) -> PublicationIn:
        if sum((bool(self.dblp_key), bool(self.doi), self.none)) != 1:
            raise ValueError("give one of dblp_key, doi or none")
        return self


class SessionIn(BaseModel):
    token: str


# -- integrations ----------------------------------------------------------------------------------


class Failing(BaseModel):
    """The event an integration is stuck on; its later events wait behind it."""

    event_id: int
    type: str | None = None
    paper_id: str | None = None
    failures: int
    retry_at: datetime | None = None  # tried again then, or at once on retry
    error: str | None = None


class SearchStats(BaseModel):
    """How a search provider has answered since the service started."""

    queries: int = 0
    left_out: int = 0  # failed or too slow, and left out of the response
    last_error: str | None = None
    last_error_at: datetime | None = None


class IntegrationCard(BaseModel):
    """One integration under Settings → Integrations."""

    name: str
    description: str | None = None
    enabled: bool  # loaded now; a configured but disabled one keeps its cursor
    lag: int = 0  # outbox events after its cursor, wanted or not
    failing: Failing | None = None
    last_error: str | None = None  # kept after a recovery
    failed_at: datetime | None = None
    skipped: int = 0
    answers_card_requests: bool = False
    search: SearchStats | None = None  # for a search provider


class CardRequested(BaseModel):
    event_id: int


class Changes(BaseModel):
    """A page of the changes feed, and the position to read on from."""

    events: list[Event]
    next: str
