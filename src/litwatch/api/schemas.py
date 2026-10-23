"""What the API returns, as types: the OpenAPI schema the web app's client is generated from."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from litwatch.core import (
    Depth,
    Event,
    Facets,
    Fit,
    Grade,
    Hand,
    LabelValue,
    Opinion,
    Publication,
    Rarity,
    ReactionReason,
    ReactionValue,
    Status,
    Weight,
)
from litwatch.core.digests import DigestPeriod, DigestRole, DigestState

# The most days one History request covers.
HISTORY_SPAN = timedelta(days=92)

# -- the status line -------------------------------------------------------------------------------


class RunStatus(BaseModel):
    """The latest nightly run: none yet, running, failed, or finished without an error, with
    papers waiting for the owner (`attention`) or not (`quiet`)."""

    state: Literal["none", "running", "failed", "attention", "quiet"]
    run_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    papers: int = 0  # classified by the run (so far, while it runs)
    waiting: int | None = None  # still to classify, while it runs
    awaiting_classification: int = 0  # papers whose classification failed three runs in a row
    analysis_failed: int = 0  # papers whose analysis failed three runs in a row, stopped
    ungraded: int = 0  # gradings that failed three runs in a row, stopped
    # full-text passes that failed three runs in a row, or await their text over a week
    fulltext_failed: int = 0
    digests_failed: int = 0  # digests whose writer call failed, written again by the next run
    over_budget: list[str] = Field(default_factory=list)  # providers projected over budget


# -- topics and rows -------------------------------------------------------------------------------


class SettingsHints(BaseModel):
    """What the Settings sidebar says beside each page: how many things wait on the owner, the
    last Calibrate session, the misses reported, the topics followed and the classes, the
    month's spend against its budget (the budgeted providers' spend, or every provider's when
    none has a budget), and the integrations failing."""

    waiting: int
    last_session: datetime | None = None
    misses: int
    topics: int
    classes: int
    spent: float
    budget: float | None = None
    failing: list[str] = Field(default_factory=list)


class Summary(BaseModel):
    """What the shell shows on every page (the status line, the sidebar's topics, the Settings
    hints), read by one poll and never through a page's own reads: counts only."""

    status: RunStatus
    topics: list[Badge]  # the shown topics in the fixed order
    settings: SettingsHints


class Badge(BaseModel):
    """A followed topic as a badge shows it: its class's slug and name, its depth (the badge's
    shape), monogram and colour."""

    slug: str
    name: str
    depth: Depth
    monogram: str
    colour: str


class GradedBadge(Badge):
    """A badge on a paper, one per followed topic whose page holds it: the grade and rarity
    that page shows (`core.page_standing`); grade null where neither the topic nor a followed
    descendant graded it, the rarity then its Weight's floor."""

    grade: Grade | None
    rarity: Rarity


class PaperRef(BaseModel):
    id: str
    title: str


# A row's open state: `word` asks for the owner's word (an unlabelled split, escalation or
# central or related grade from one lab),
# `failure` reports a step stopped after three failed runs; `word` when both.
type RowState = Literal["word", "failure"]


class Row(BaseModel):
    """One paper in a list. `rarity` is the list's: the mixed list's on Papers, the topic page's
    on a topic. No Gist, Weight or facets until analysis succeeds; no rarity until classified."""

    id: str
    title: str
    authors: list[str]
    published: datetime | None = None
    gist: str | None = None  # the current one
    weight: Weight | None = None  # the current one
    facets: Facets | None = None
    rarity: Rarity | None = None
    badges: list[GradedBadge] = Field(default_factory=list)  # the highest level's topic first
    saved: bool = False
    state: RowState | None = None


class TopicRow(Row):
    """A paper on a topic's page: the topic's own grade, else the highest of the followed topics
    under it (`via`), else none (ungraded there, coloured by its Weight)."""

    grade: Grade | None = None
    via: str | None = None


class OutsideRow(BaseModel):
    """A paper a search provider found that the collection lacks."""

    id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    published: datetime | None = None
    source: str  # the provider


class RarityCounts(BaseModel):
    """Papers per rarity level; `unrated` have none yet (not classified)."""

    legendary: int = 0
    epic: int = 0
    rare: int = 0
    uncommon: int = 0
    common: int = 0
    unrated: int = 0


class GradeCounts(BaseModel):
    """Papers per grade on a topic's page; `ungraded` have none there."""

    must_read: int = 0
    relevant: int = 0
    adjacent: int = 0
    none: int = 0
    ungraded: int = 0


type Sort = Literal["rarest", "newest", "added", "title", "best"]
type GradeLevel = Literal["must_read", "relevant", "adjacent", "none", "ungraded"]


class ListQuery(BaseModel):
    """A list's filters, as query parameters. Rarity and grade are the levels: several may be
    given, and the counts in the answer leave them out. A grade on Papers is a badge's; on a
    topic page the page's own. `rarest` sorts by rarity, then newest first; `best` by search
    rank while `q` is given, else as `rarest`. Omitted, the sort is `best` while `q` is given,
    else `rarest`."""

    q: str | None = None
    rarity: list[Rarity] = Field(default_factory=list)
    grade: list[GradeLevel] = Field(default_factory=list)
    saved: bool | None = None
    code: bool | None = None  # the abstract announces released code
    weight: list[Weight] = Field(default_factory=list)
    since: date | None = None  # published on or after (UTC)
    until: date | None = None  # published on or before (UTC)
    sort: Sort | None = Field(
        default=None,
        description="Omitted: `best` (search rank) while `q` is given, else `rarest`.",
    )

    @model_validator(mode="after")
    def _sort_by_default(self) -> ListQuery:
        if self.sort is None:
            self.sort = "best" if self.q else "rarest"
        return self


class PaperQuery(ListQuery):
    """Papers' filters: also the pages of one or more topics (each with its subtree), or of
    every topic at a depth, and one band of a search."""

    topic: list[str] = Field(default_factory=list)
    depth: Depth | None = None
    source: Literal["collection", "outside"] | None = None


class Paged(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class PapersIn(PaperQuery, Paged):
    pass


class TopicPapersIn(ListQuery, Paged):
    pass


class Papers(BaseModel):
    """A page of Papers, the whole filter's total, and the papers per rarity under every filter
    but the levels. A search also brings what search providers found that the collection lacks,
    as a band of its own on the first page; a provider that failed or was too slow is named in
    `left_out`."""

    rows: list[Row]
    total: int
    counts: RarityCounts
    outside: list[OutsideRow] = Field(default_factory=list)
    left_out: list[str] = Field(default_factory=list)


class StripDigest(BaseModel):
    """One square of a topic's digest strip: a digest, coloured by its best rarity (none for
    nothing new), red when it failed."""

    id: int
    day: date  # a weekly digest's Monday
    state: DigestState
    best_rarity: Rarity | None = None


class TopicHead(Badge):
    status: Status
    subject: str
    # The topic's latest digests, oldest first: daily for Focus and Area, weekly for Field.
    strip: list[StripDigest] = Field(default_factory=list)


class TopicCard(TopicHead):
    """A topic on the Topics tab: its page's papers, how many are saved, how many at each of
    the page's rarity levels, and its newest must read."""

    papers: int
    saved: int
    counts: RarityCounts
    newest_must_read: PaperRef | None = None


class TopicGroup(BaseModel):
    depth: Depth
    topics: list[TopicCard]  # in the owner's order


class TopicPage(BaseModel):
    """A page of a topic's papers, its total, and the papers per rarity and per grade under
    every filter but the levels."""

    topic: TopicHead
    rows: list[TopicRow]
    total: int
    counts: RarityCounts
    grades: GradeCounts


# -- a paper ---------------------------------------------------------------------------------------


class Version(BaseModel):
    version: int
    date: datetime | None = None


class Gist(BaseModel):
    text: str
    source: Literal["abstract", "full_text"] = "abstract"  # full text: written with the Brief


class Brief(BaseModel):
    """The paper's Brief: its current revision's, or the newest from an older revision
    (`revision` says which) until the current one is written. `overstatement`, when the full
    text does not support the abstract, opens it."""

    id: int
    text: str
    overstatement: str | None = None
    revision: int
    arxiv_version: int | None = None  # of the text it read; null when the archive could not say
    source: Literal["latex", "pdf"]
    written_at: datetime


class TextPass(BaseModel):
    """The full-text pass queued last for the paper's current text: a Brief (Focus) or a Weight
    re-check with a Brief (Area pick candidate), and where it is. A paper without one shows its
    abstract in the Brief's place."""

    id: int
    kind: Literal["brief", "weight"]
    state: Literal["awaiting_text", "ready", "running", "done", "failed"]
    queued_at: datetime


class WeightState(BaseModel):
    weight: Weight
    reason: str
    source: Literal["abstract", "full_text"]


class CurrentVerdict(BaseModel):
    """A topic's current verdict: the Fit that stands, each grader's answer by role, and the
    paper revision it was made from. `one_lab`: one Focus grader refused throughout and the
    other's Fit stands."""

    fit: Fit
    reason: str
    split: bool = False
    escalated: bool = False
    one_lab: bool = False
    opinions: dict[str, Opinion] = Field(default_factory=dict)
    paper_revision: int = 1
    recorded_at: datetime | None = None


class LabelState(BaseModel):
    value: LabelValue
    origin: str
    provisional: bool = False


class ReactionState(BaseModel):
    value: ReactionValue
    reason: ReactionReason | None = None


# A topic's open state on a paper: an unlabelled split, escalation or central or related grade
# from one lab (`one_lab`) asks for the owner's word; `ungraded` is a grading stopped after three
# failed runs or refused by every model (Grade it now); `grading_again` a grading that failed
# and is tried again.
type TopicState = Literal["split", "escalated", "one_lab", "ungraded", "grading_again"]


class PaperTopic(BaseModel):
    """A followed topic the paper is in, graded for, or has the owner's word or a thumb on: its
    own effective Fit, how its page shows the paper (grade, rarity, `via`: as the topic page),
    its current verdict, the owner's word that stands (from the label, a thumb or a miss), the
    label and thumb themselves, and its open state."""

    topic: Badge
    fit: Fit | None = None  # the effective one, on this topic itself
    grade: Grade | None = None
    rarity: Rarity | None = None
    via: str | None = None
    verdict: CurrentVerdict | None = None
    word: Fit | None = None
    label: LabelState | None = None
    reaction: ReactionState | None = None
    missed: bool = False  # reported as missed for this topic
    state: TopicState | None = None


class OpenState(BaseModel):
    """What waits on the owner for this paper, as Needs you lists it, with the paper revision
    the action endpoints take: `awaiting_classification` (dismiss, classify by hand, fix the
    text), `analysis_stopped` (try again), a topic's `split`, `escalated`, `one_lab` (label it)
    or `ungraded` (grade it now), and a full-text pass (`pass_id`) `fulltext_failed`, a Deep
    card's `card_failed`, or either `awaiting_text` over a week (run it now). `refused`: every
    model refused, rather than the last error."""

    kind: Literal[
        "awaiting_classification",
        "analysis_stopped",
        "split",
        "escalated",
        "one_lab",
        "ungraded",
        "fulltext_failed",
        "card_failed",
        "awaiting_text",
    ]
    revision: int
    topic: str | None = None
    error: str | None = None
    refused: bool = False
    pass_id: int | None = None


class ClassSource(BaseModel):
    source: Literal["classifier", "uncertain", "keyword", "owner", "miss"]
    member: bool = True  # false only for the owner's label taking the paper out
    probability: float | None = None


class PaperClass(BaseModel):
    """A class the paper has a membership of, by source. `member`: one stands that puts the
    paper in the class (the uncertain band and the keyword check are for grading only). `path`
    is its classes' names from the top down to it."""

    slug: str
    name: str
    path: list[str]
    member: bool
    sources: list[ClassSource]


class Classification(BaseModel):
    """The newest classification and the current memberships, classes of the current tree only
    (a retired class leads nowhere)."""

    primary: str
    primary_name: str
    model: str
    at: datetime
    paper_revision: int
    classes: list[PaperClass]


class HistoryItem(BaseModel):
    """Something said about the paper, oldest first: every verdict (`value` its Fit), the
    owner's label (its value) and thumb (its value and reason) as they stand, misses, and each
    classification (`topic` is its primary class). `followed`: the topic is followed now."""

    what: Literal["graded", "labelled", "reacted", "missed", "classified"]
    at: datetime
    topic: str | None = None
    name: str | None = None  # the topic's or class's
    followed: bool = False
    value: str | None = None
    reason: ReactionReason | None = None
    revision: int | None = None
    split: bool | None = None
    escalated: bool | None = None


RELATIONS = ("agrees", "contradicts", "extends", "supersedes")


class AmongRow(BaseModel):
    """A paper a Deep card places this one among: one of the collection's, by its arXiv id, with
    how the two relate and its rarity now (null outside the collection)."""

    id: str
    title: str
    relation: Literal["agrees", "contradicts", "extends", "supersedes"]
    note: str | None = None
    rarity: Rarity | None = None
    in_collection: bool = False


class DeepCard(BaseModel):
    """A Deep card: the reading card the deep service wrote, as it wrote it, and what it was
    written from (the paper revision and arXiv version, null where unknown, and the text's
    source). `among` is its "among the collection" section as rows."""

    card: dict[str, Any]
    model: str | None = None
    written_at: datetime | None = None
    revision: int | None = None
    arxiv_version: int | None = None
    source: Literal["latex", "pdf"] | None = None
    among: list[AmongRow] = Field(default_factory=list)


class CardPass(BaseModel):
    """The paper's Deep card in the full-text queue: the owner's request or automatic (a Focus
    must read, or an Area pick), and where it is (awaiting the paper's text, ready, being
    written, or failed this run and to be tried again; `failed_runs` says how often)."""

    id: int
    reason: Literal["request", "focus", "pick"]
    state: Literal["awaiting_text", "ready", "running", "failed"]
    queued_at: datetime
    failed_runs: int = 0
    error: str | None = None


JobState = Literal["offered", "stopped", "tonight", "queued", "running", "failed", "done"]


class ProcessJob(BaseModel):
    """A paper's Process job (DESIGN.md, the paper page): its button reads "Processing" while
    the job is queued or running. `again`: failed, and Process would put it back in the queue,
    since no step of its paper waits on the owner (then the owner's action there settles it)."""

    id: int
    state: JobState
    error: str | None = None
    again: bool = False


class Paper(BaseModel):
    """A paper's page: its header, its ladder (Gist, Brief or abstract, deep card, the paper)
    and the rail, its open states and its history. Outside the collection (never classified)
    it has no rarity; an archive paper litwatch has not fetched is not `stored` either, and has
    only the archive's metadata."""

    id: str
    title: str
    authors: list[str]
    abstract: str
    published: datetime | None = None
    updated: datetime | None = None
    version: int | None = None  # the arXiv version stored, when known
    versions: list[Version] = Field(default_factory=list)  # arXiv versions seen
    revision: int = 1  # the paper's own: raised by each rewrite of title or abstract
    arxiv_url: str
    pdf_url: str  # litwatch's own: the archive's copy, else arXiv's (GET /api/papers/{id}/pdf)
    source_url: str  # the LaTeX source
    in_collection: bool
    stored: bool = True  # litwatch holds it: fetched
    next_run: bool = False  # the next run classifies it: not dismissed, its text not yet classified
    rarity: Rarity | None = None
    badges: list[GradedBadge] = Field(default_factory=list)
    gist: Gist | None = None
    brief: Brief | None = None
    text_pass: TextPass | None = None
    weight: WeightState | None = None
    facets: Facets | None = None
    saved: bool = False
    hand: Hand | None = None  # the owner's newest use of the Save toggle
    topics: list[PaperTopic]  # the rail, in the fixed topic order
    reaction: ReactionState | None = None  # for no topic (a plain search result)
    classification: Classification | None = None
    states: list[OpenState] = Field(default_factory=list)
    history: list[HistoryItem] = Field(default_factory=list)
    publication: Publication | None = None  # the published version, when one is known
    publication_by_owner: bool = False  # the owner chose it (or chose "none")
    card: DeepCard | None = None  # shown whenever one is stored, integration or not
    card_pass: CardPass | None = None  # queued or being written, while a card provider is loaded
    # A card provider is loaded and no card is queued or being written: "Deep read", or "Write
    # again" under a card.
    card_requestable: bool = False
    process: ProcessJob | None = None  # the paper's newest Process job, when it has one


# -- what the owner says ---------------------------------------------------------------------------


class ReactionIn(BaseModel):
    topic: str | None = None  # the topic of the surface it was given on; none in a plain search
    value: ReactionValue
    reason: ReactionReason | None = None


class LabelIn(BaseModel):
    value: LabelValue


class SaveIn(BaseModel):
    saved: bool


class Ack(BaseModel):
    """What an action changed. The page shows the result where it was taken; `message` is set
    only for what it cannot show (DESIGN.md, acknowledgement): a thumb that gives or takes back
    the owner's word on a Focus topic, and a reported miss."""

    message: str | None = None
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
    fit: Literal[Fit.CENTRAL, Fit.RELATED] = Fit.CENTRAL  # the owner's word, asked at once


class CitedIn(BaseModel):
    """A paper outside the collection, as a search hit gives it: enough for an arXiv entry."""

    id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None


class BibtexIn(BaseModel):
    ids: list[str] = Field(default_factory=list)  # papers in the collection
    filter: PaperQuery | None = None  # every collection paper a Papers filter matches, too
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


# -- digests --------------------------------------------------------------------------------------


class DigestCounts(BaseModel):
    """What a digest's Mark counts: the papers it covered, its picks, and of them those central,
    related, and graded must read; apart from them, its updated papers."""

    papers: int = 0
    picks: int = 0
    central: int = 0
    related: int = 0
    must_read: int = 0
    updated: int = 0


class DigestMark(BaseModel):
    """A digest at Mark level (History): its topic, how many papers, its best rarity."""

    id: int
    topic: Badge
    period: DigestPeriod
    day: date  # a weekly digest's Monday
    state: DigestState
    counts: DigestCounts
    best_rarity: Rarity | None = None


class DigestPaper(BaseModel):
    """A paper as a digest showed it, frozen then; `set_aside` is live: the owner has since set
    it below relevant where it was graded. An updated paper has the arXiv `version` it came
    back with; its `announced` is when it first came out."""

    id: str
    title: str
    announced: date | None = None
    role: DigestRole
    version: int | None = None
    why: str | None = None  # a pick's reason
    revision: int
    gist: str | None = None
    fit: Fit | None = None
    grade: Grade | None = None
    rarity: Rarity
    set_aside: bool = False


class DigestCard(DigestMark):
    """A digest at Gist level (a Day or a Week): its Gist and up to three of its papers (its
    picks, else its central papers); `more` are the rest."""

    gist: str | None = None
    papers: list[DigestPaper]
    more: int


class Digest(DigestMark):
    """A digest in full: its text, which marks each paper it names as [the name](its arXiv
    id), and every paper by role; why it failed, when it did."""

    gist: str | None = None
    text: str | None = None
    error: str | None = None
    written_at: datetime
    papers: list[DigestPaper]


class TodayRun(BaseModel):
    """Where today's nightly run stands: `waiting` until it is due, `late` once it is overdue
    and has not started, `running` (`overrun` past the night's deadline) with the papers read
    of `total`, `failed` with its error, or `done`. `due` and `deadline` are tonight's."""

    state: Literal["waiting", "late", "running", "overrun", "failed", "done"]
    due: datetime
    deadline: datetime
    papers: int = 0
    total: int | None = None
    error: str | None = None


class Day(BaseModel):
    """A day's daily digests in the fixed topic order. `released` is false on a day arXiv listed
    nothing; `previous` and `next` are the nearest days with digests; on a Monday, `weekly` is
    the Monday of the week whose weekly digests came out that day. `run` is today's nightly run,
    on today only (in the schedule's zone) and only while the schedule is on."""

    day: date
    released: bool
    digests: list[DigestCard]
    previous: date | None = None
    next: date | None = None
    weekly: date | None = None
    run: TodayRun | None = None


class Week(BaseModel):
    """A week's weekly digests in the fixed topic order, keyed by its Monday."""

    monday: date
    digests: list[DigestCard]
    previous: date | None = None
    next: date | None = None


class HistoryDay(BaseModel):
    day: date
    released: bool
    marks: list[DigestMark]


class HistoryWeek(BaseModel):
    monday: date
    marks: list[DigestMark]


class History(BaseModel):
    """Days from `until` back to `since`, newest first, and the weeks they touch; `start` is the
    day of the oldest digest, where History stops."""

    days: list[HistoryDay]
    weeks: list[HistoryWeek]
    start: date | None = None


class HistoryIn(BaseModel):
    since: date
    until: date

    @model_validator(mode="after")
    def _span(self) -> HistoryIn:
        if not timedelta(0) <= self.until - self.since <= HISTORY_SPAN:
            raise ValueError(f"since must be before until, at most {HISTORY_SPAN.days} days")
        return self


# -- integrations ----------------------------------------------------------------------------------


class Failing(BaseModel):
    """The event an integration is stuck on; its later events wait behind it."""

    event_id: int
    type: str | None = None
    paper_id: str | None = None
    failures: int
    retry_at: datetime | None = None  # tried again then, or at once on retry
    error: str | None = None


class SetupFailing(BaseModel):
    """The integration's setup is failing: no event is handed to it until it succeeds."""

    failures: int
    retry_at: datetime | None = None  # tried again then, or at once on retry
    error: str | None = None
    failed_at: datetime | None = None


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
    setup_failing: SetupFailing | None = None
    last_error: str | None = None  # kept after a recovery
    failed_at: datetime | None = None
    skipped: int = 0
    answers_card_requests: bool = False
    search: SearchStats | None = None  # for a search provider


class SearchScope(BaseModel):
    """What the search box reaches beyond the collection: the papers the archive's search index
    holds, None without an archive or when it does not answer in time."""

    archive_papers: int | None = None


class CardRequested(BaseModel):
    pass_id: int  # the Deep pass that writes the card


class Changes(BaseModel):
    """A page of the changes feed, and the position to read on from."""

    events: list[Event]
    next: str


# -- jobs ------------------------------------------------------------------------------------------

JobAction = Literal["run", "tonight", "discard", "again"]


class JobPaper(BaseModel):
    id: str
    title: str


RerunScope = Literal["labels", "members", "keywords", "range"]


class JobTopic(BaseModel):
    """A re-run's topic, and the revision of it the re-run was offered for."""

    slug: str
    name: str
    revision: int | None = None


class Job(BaseModel):
    """One row of the Jobs page (DESIGN.md, Jobs): a job (`id`), or a Deep card the owner asked
    for (`pass_id`), read from its pass with the pass's state as a job's (awaiting text or ready
    is queued). `papers` and `done` say how far it got (a re-run's `graded` how many it graded);
    `cost` is what its runs spent so far, None before any and for a card, which runs on the
    subscription. `at` is when it last changed state. A re-run has its topic, `scopes`, the
    `estimate` it was offered with and its `ceiling`; stopped by the ceiling, `error` says where.
    `actions` are those its state allows: Run now, With tonight's run and Discard while it is
    not run; Try again once it failed, for a card only once it waits for the owner (a card that
    failed fewer runs is tried again by the next nightly run)."""

    key: str  # unique across jobs and cards
    kind: Literal["process", "rerun", "deep"]
    id: int | None = None
    pass_id: int | None = None
    state: JobState
    paper: JobPaper | None = None
    topic: JobTopic | None = None
    scopes: list[RerunScope] = []
    papers: int
    done: int
    graded: int | None = None
    cost: float | None = None
    estimate: float | None = None
    ceiling: float | None = None
    error: str | None = None
    at: datetime
    actions: list[JobAction] = []


class Jobs(BaseModel):
    """The Jobs page's four groups: Not run, Running and queued, Failed, and the newest Done."""

    not_run: list[Job]
    running: list[Job]
    failed: list[Job]
    done: list[Job]


class ProcessIn(BaseModel):
    paper_id: str  # an arXiv id


class RerunScopeRow(BaseModel):
    """One scope of the re-run offer: its papers and what grading them is estimated to cost
    (None when a role has no calls to estimate by)."""

    scope: RerunScope
    papers: int
    estimate: float | None = None


class RerunOffer(BaseModel):
    """The re-run offer after a topic edit (DESIGN.md, the re-run offer): each scope with its
    paper count and estimate; `papers` and `estimate` for the scopes chosen, a paper in two of
    them counted once. `missing` are the roles with no calls to estimate by; `unpriced` the
    models (`role: model`) with no price bound, whose calls a re-run does not send. `ceiling`
    is the one offered, in USD."""

    topic: JobTopic
    rows: list[RerunScopeRow]
    papers: int
    estimate: float | None = None
    missing: list[str] = []
    unpriced: list[str] = []
    ceiling: float


class RerunIn(BaseModel):
    """A re-run to start (`run`) or to keep under Not run: its topic, its scopes (the date
    range's days in the schedule's zone, both included) and its ceiling in USD."""

    topic: str
    scopes: list[RerunScope] = Field(min_length=1)
    since: date | None = None
    until: date | None = None
    ceiling: float = Field(gt=0)
    run: bool


class JobRun(BaseModel):
    """Run now or With tonight's run: a re-run's new ceiling in USD, which a stopped one needs
    above what it spent."""

    ceiling: float | None = Field(default=None, gt=0)
