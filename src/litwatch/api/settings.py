"""Settings, the kitchen behind the gear: the taxonomy and its topics, Quality and the papers
awaiting classification, Calibrate, the misses, runs, and the configuration (read-only, spec
decision 11)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ValidationError

from litwatch import classification, grading, seeds
from litwatch.api.deps import Conn, budget_lines
from litwatch.core import FAILED_RUNS, Fit, Paper, Status, Taxonomy, Topic
from litwatch.core.budget import month_start
from litwatch.core.classification import thresholds
from litwatch.core.grading import GRADERS
from litwatch.feedback import calibrate, word
from litwatch.jobs import scheduler
from litwatch.jobs.context import model_run
from litwatch.store import digests as digests_store
from litwatch.store import failures, quality
from litwatch.store import fulltext as fulltext_store
from litwatch.store import integrations as integrations_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store

router = APIRouter(prefix="/api/settings")

SECRET_WORDS = ("key", "token", "password", "secret", "authorization", "credential")


# -- the taxonomy ----------------------------------------------------------------------------------


@router.get("/taxonomy")
async def taxonomy(c: Conn) -> Taxonomy | None:
    """The class tree and the followed topics; null before one is imported."""
    return await taxonomy_store.load(c)


@router.get(
    "/taxonomy.yaml",
    response_class=PlainTextResponse,
    responses={404: {"description": "No taxonomy yet"}},
)
async def taxonomy_yaml(c: Conn) -> str:
    """The taxonomy as YAML, in the format `litwatch taxonomy import` reads (Export YAML)."""
    tree = await taxonomy_store.load(c)
    if tree is None:
        raise HTTPException(status_code=404, detail="no taxonomy yet")
    return seeds.dump_taxonomy(tree)


class TaxonomyStats(BaseModel):
    """What the Taxonomy and Topics pages show beside the tree: papers standing in each class or
    under it, the threshold each class is admitted at (its own, or the configured default for
    its depth), and the current revision of each followed topic."""

    members: dict[str, int]
    thresholds: dict[str, float]
    topic_revisions: dict[str, int]


def _thresholds(request: Request, tree: Taxonomy | None) -> dict[str, float]:
    """The threshold each class is admitted at, under the loaded configuration."""
    return thresholds(tree, request.app.state.config.classification.thresholds) if tree else {}


@router.get("/taxonomy/stats")
async def taxonomy_stats(request: Request, c: Conn) -> TaxonomyStats:
    tree = await taxonomy_store.load(c)
    members = await quality.class_members(c, tree) if tree else {}
    revisions = await quality.topic_revisions(c)
    return TaxonomyStats(
        members=members, thresholds=_thresholds(request, tree), topic_revisions=revisions
    )


@router.get("/topics/{slug}/keywords", responses={404: {"description": "No such topic"}})
async def keyword_matches(slug: str, c: Conn) -> dict[str, int]:
    """How many papers of the last 90 days each of the topic's keywords matches (the editor)."""
    tree = await taxonomy_store.load(c)
    topic = tree.topic(slug) if tree else None
    if topic is None:
        raise HTTPException(status_code=404, detail="no such topic")
    return await quality.keyword_matches(c, topic.keywords)


@router.put("/topics/{slug}", responses={404: {"description": "No such class"}})
async def save_topic(slug: str, body: dict[str, Any], c: Conn) -> Topic:
    """Follow class `slug`, or edit its topic. The class takes the subject as its description.
    Edits apply forward; a change of depth or content settles the topic's papers again (see
    feedback.word.save_taxonomy)."""
    async with c.transaction():
        await taxonomy_store.lock(c)
        tree = await taxonomy_store.load(c)
        if tree is None or slug not in {cls.slug for cls in tree.classes}:
            raise HTTPException(status_code=404, detail="no such class")
        try:
            topic = Topic.model_validate(body | {"class": slug})
            changed = tree.following(topic)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=exc.errors(include_url=False, include_context=False)
            ) from exc
        await word.save_taxonomy(c, changed)
    return topic


class Order(BaseModel):
    slugs: list[str]


@router.put("/topics-order", status_code=204)
async def order(body: Order, c: Conn) -> None:
    """The owner's order, which topic lists and cards follow. The list must name every topic
    once."""
    try:
        await word.reorder(c, body.slugs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# -- quality ---------------------------------------------------------------------------------------


@router.get("/quality")
async def quality_page(request: Request, c: Conn) -> dict[str, Any]:
    """At the top, what last night left; below, how well the Focus topics are doing."""
    night = await quality.last_night(c)
    since = quality.splits_window(night)
    return {
        "night": {
            "run": night["id"] if night else None,
            "error": night["error"] if night else None,
            "splits": await quality.splits_since(c, since),
            # Classification failed three runs in a row: these wait for the owner.
            "awaiting_classification": await failures.stuck(c, "classification", FAILED_RUNS),
            # Analysis failed three runs in a row; it is not tried again until the owner says.
            "analysis_failed": await failures.stuck(c, "analysis", FAILED_RUNS),
            # A grading failed three runs in a row: ungraded there until the owner grades it.
            "ungraded": await failures.stuck(c, "grading", FAILED_RUNS),
            # A full-text pass failed three runs in a row, or awaits its text over a week.
            "fulltext": await fulltext_store.stuck(c, FAILED_RUNS),
            # A digest whose writer call failed: the next run writes it again.
            "digests": await digests_store.failed(c),
        },
        "cost": [
            line.model_dump() for line in await budget_lines(c, request.app.state.config.budget)
        ],
        # Deep cards' month to date at list prices, observed and not budgeted: a lower bound
        # while any attempt's usage is unknown.
        "deep": await integrations_store.usage_since(c, month_start(datetime.now(UTC))),
        "recall": await quality.replay_recall(c),
        "calibration": await calibrate.precision(c),
        "matters_no_topic": await calibrate.matters_no_topic(c),
        "collected": await quality.collected_reactions(c),
        "reactions": await quality.reactions_by_topic(c),
    }


class StepSeen(BaseModel):
    """A stopped step as Needs you showed it: the paper, its revision then, a grading's topic."""

    step: failures.Step
    paper_id: str
    revision: int
    topic: str | None = None


class AcknowledgeIn(BaseModel):
    """What the owner acknowledged: stopped steps, full-text passes and failed digests (by id)."""

    steps: list[StepSeen] = []
    passes: list[int] = []
    digests: list[int] = []


@router.post("/acknowledge", status_code=204)
async def acknowledge(body: AcknowledgeIn, c: Conn) -> None:
    """Acknowledge on Needs you: the owner has seen these items, and they stop counting in the
    run status. An item is marked only while it still waits on the owner as shown (a step on
    the revision named); one that recovers and fails again comes back unacknowledged. It moves
    no paper. A failed nightly run is not an item and is never acknowledged."""
    steps = [(x.step, x.paper_id, x.topic or "", x.revision) for x in body.steps]
    async with c.transaction():
        await failures.acknowledge(c, steps, FAILED_RUNS)
        await fulltext_store.acknowledge(c, body.passes, FAILED_RUNS)
        await digests_store.acknowledge(c, body.digests)


# -- misses ----------------------------------------------------------------------------------------


class Grader(BaseModel):
    """One grader's answer in the verdict: its role, and its Fit, or for `area_check` whether
    the judge's Fit holds."""

    role: str
    fit: Fit | None = None
    holds: bool | None = None


class MissTrace(BaseModel):
    """What each stage concluded for the owner's topic when the reported paper went through the
    pipeline (feedback.md, misses), on the paper's current text. `classified`: classification
    has run; then `probability` is the classifier's for the topic's class against the class's
    `threshold`, and `classifier`, `uncertain` and `keyword` say which way in admitted it;
    `below` says the classifier admitted it to a class under the topic's only, not to the
    topic's own class.
    `graded`: a verdict exists; then `fit` is the Fit that stands by the rule of the depth it
    was graded at (core.grading), and `graders` the answers it stands on, in the order the
    topic's current depth asks its graders (the roles a verdict from another depth holds
    after). The page reads where the paper was let go from `fit`, not from the graders."""

    classified: bool
    probability: float | None = None
    threshold: float
    classifier: bool | None = None
    below: bool | None = None
    uncertain: bool | None = None
    keyword: bool | None = None
    graded: bool
    fit: Fit | None = None
    graders: list[Grader] = []


class Miss(BaseModel):
    id: int
    paper_id: str
    title: str
    topic: str
    reported_at: datetime
    trace: MissTrace


@router.get("/misses")
async def misses(request: Request, c: Conn) -> list[Miss]:
    """Reported misses, newest first, each with its trace."""
    tree = await taxonomy_store.load(c)
    at = _thresholds(request, tree)
    found = []
    for r in await quality.misses(c, tree):
        opinions = r["opinions"] or {}
        topic = tree.topic(r["topic"]) if tree else None
        asked = GRADERS[topic.depth] if topic else ()
        roles = [*asked, *(k for k in opinions if k not in asked)]
        sources = set(r["sources"])
        classified = r["classified"]
        found.append(
            Miss(
                id=r["id"],
                paper_id=r["paper_id"],
                title=r["title"],
                topic=r["topic"],
                reported_at=r["reported_at"],
                trace=MissTrace(
                    classified=classified,
                    probability=r["probability"],
                    threshold=at.get(r["topic"], classification.CHOSEN.default),
                    classifier=bool(sources & {"classifier", "below"}) if classified else None,
                    below="below" in sources and "classifier" not in sources
                    if classified
                    else None,
                    uncertain="uncertain" in sources if classified else None,
                    keyword="keyword" in sources if classified else None,
                    graded=r["opinions"] is not None,
                    fit=r["verdict_fit"],
                    graders=[
                        Grader(role=k, fit=opinions[k].get("fit"), holds=opinions[k].get("holds"))
                        for k in roles
                        if k in opinions
                    ],
                ),
            )
        )
    return found


# -- papers classification kept failing on (pipeline.md, section 9) -------------------------------


class Awaiting(BaseModel):
    """What every action on a paper awaiting classification names: the paper revision the
    owner saw on Quality. The action applies only to that text."""

    revision: int


# What `_awaiting` answers besides success, for the API schema.
WAITING = {
    404: {"description": "No such paper"},
    409: {"description": "The paper's text changed, or it no longer waits for the owner"},
}


@asynccontextmanager
async def _awaiting(
    c: Any, paper_id: str, revision: int, step: failures.Step = "classification", topic: str = ""
) -> AsyncIterator[Paper]:
    """The paper, in a change of it (`word.change`) that is the action's, if `step` still waits
    for the owner on it at `revision` (`failures.waiting`); 409 if its text changed or it no
    longer waits (a nightly rewrite, another action, its topic paused). Eligibility is read
    after the locks, in the transaction that acts, and the paper is settled at its end."""
    async with word.change(c, paper_id):
        paper = await papers_store.lock(c, paper_id)
        if paper is None:
            raise HTTPException(status_code=404, detail="no such paper")
        if paper.revision != revision:
            raise HTTPException(status_code=409, detail="the paper's text changed meanwhile")
        if not await failures.waiting(c, paper, step, FAILED_RUNS, topic):
            raise HTTPException(status_code=409, detail=f"{step} is not waiting for the owner")
        yield paper


@router.post("/classification/{paper_id:path}/dismiss", status_code=204, responses=WAITING)
async def dismiss(paper_id: str, body: Awaiting, c: Conn) -> None:
    """Litwatch drops the paper from processing; it stays searchable in the archive."""
    async with _awaiting(c, paper_id, body.revision):
        await papers_store.dismiss(c, paper_id)


class ClassesIn(Awaiting):
    classes: list[str]  # class slugs, the primary one first unless `primary` names it
    primary: str | None = None


@router.put("/classification/{paper_id:path}", responses=WAITING)
async def classify_by_hand(paper_id: str, body: ClassesIn, c: Conn) -> dict[str, Any]:
    """The owner names the paper's classes, which go on as usual with source `owner`."""
    # The change's shared taxonomy lock holds the tree still until the classification is
    # recorded, and the paper is classified and settled together.
    async with _awaiting(c, paper_id, body.revision) as paper:
        tree = await taxonomy_store.load(c)
        if tree is None:
            raise HTTPException(status_code=409, detail="no taxonomy")
        try:
            await classification.by_hand(c, paper, tree, body.classes, body.primary)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "classes": list(dict.fromkeys(body.classes)),
        "primary": body.primary or body.classes[0],
    }


class CorrectionIn(Awaiting):
    title: str | None = None
    abstract: str | None = None


@router.post("/classification/{paper_id:path}/correction", status_code=204, responses=WAITING)
async def correct(paper_id: str, body: CorrectionIn, c: Conn) -> None:
    """Fix the text classification choked on. It counts as a rewrite: the next run classifies
    the corrected paper."""
    title = (body.title or "").strip() or None
    abstract = (body.abstract or "").strip() or None
    if not (title or abstract):
        raise HTTPException(status_code=422, detail="give a title or an abstract")
    # The new text and the grades without its old Weight, together.
    async with _awaiting(c, paper_id, body.revision):
        await papers_store.correct(c, paper_id, title, abstract)


@router.post("/analysis/{paper_id:path}/retry", status_code=204, responses=WAITING)
async def retry_analysis(paper_id: str, body: Awaiting, c: Conn) -> None:
    """Analysis failed three runs in a row on this text: give it three more, from the next run."""
    async with _awaiting(c, paper_id, body.revision, "analysis"):
        await failures.succeeded(c, paper_id, "analysis")  # forgets the count


class GradedOut(BaseModel):
    """What grading now found: the Fit that stands, or why there is no verdict."""

    graded: bool
    fit: Fit | None = None
    error: str | None = None


@router.post("/grading/{topic}/{paper_id:path}/now", responses=WAITING)
async def grade_now(
    topic: str, paper_id: str, body: Awaiting, request: Request, c: Conn
) -> GradedOut:
    """A grading failed three runs in a row on this text: grade it now, as a run of its own.
    It counts as the first of three more runs. Until it is recorded the grading stays a pending
    failure, so a request cut short leaves it to the next nightly run; 409 when the paper's text
    changed, it was dismissed or the topic was paused while it was graded."""
    async with word.change(c, paper_id):  # the topic read under the taxonomy lock, shared
        tree = await taxonomy_store.load(c)
        followed = tree.topic(topic) if tree else None
        if tree is None or followed is None:
            raise HTTPException(status_code=404, detail="no such topic")
        if followed.status != Status.ACTIVE:
            raise HTTPException(status_code=409, detail=f"{topic} is {followed.status}: not graded")
        async with _awaiting(c, paper_id, body.revision, "grading", topic) as paper:
            await failures.reset(c, paper_id, "grading", topic)
    config = request.app.state.config
    due = [grading.Due(paper, followed, tree.get(topic).name)]
    recorded = False
    async with (
        model_run(c, config, "grading", gateway=request.app.state.gateway) as run,
        grading.grade(due, run.gateway, config.grading) as found,
    ):
        async for g in found:
            if recorded := await grading.record(c, g):
                run.add("graded" if g.verdict is not None else "grading_failed")
    if not recorded:
        raise HTTPException(
            status_code=409, detail="the paper's text changed, or the topic was paused, meanwhile"
        )
    v = g.verdict
    return GradedOut(graded=v is not None, fit=v.fit if v else None, error=g.error)


# What `run_pass_now` answers besides success, for the API schema.
PASS_WAITING = {
    404: {"description": "No such full-text pass"},
    409: {"description": "The pass no longer waits for the owner"},
}


@router.post("/fulltext/{pass_id}/now", status_code=204, responses=PASS_WAITING)
async def run_pass_now(pass_id: int, request: Request, c: Conn) -> None:
    """A full-text pass (a Deep card's included) failed three runs in a row, or awaits its text
    over a week: run it now. It is first in line for its worker, with three more runs; its text
    is asked for again at urgent priority. 409 when it no longer waits for the owner (it ran,
    the paper's text changed, it was dropped)."""
    await _run_now(pass_id, request, c, pdf=False)


@router.post("/fulltext/{pass_id}/pdf", status_code=204, responses=PASS_WAITING)
async def use_pdf(pass_id: int, request: Request, c: Conn) -> None:
    """ "Use the PDF", for a paper whose LaTeX reads badly: run the pass now, as above, reading
    the paper's PDF from then on instead of its LaTeX."""
    await _run_now(pass_id, request, c, pdf=True)


async def _run_now(pass_id: int, request: Request, c: Any, *, pdf: bool) -> None:
    paper_id = await fulltext_store.paper_of(c, pass_id)  # a pass never changes paper
    if paper_id is None:
        raise HTTPException(status_code=404, detail="no such full-text pass")
    async with word.change(c, paper_id):  # as every write to a paper's passes
        if not await fulltext_store.run_now(c, pass_id, FAILED_RUNS, pdf=pdf):
            raise HTTPException(status_code=409, detail="the pass is not waiting for the owner")
    request.app.state.fulltext_wake.set()
    request.app.state.deep_wake.set()


# -- calibrate -------------------------------------------------------------------------------------


class DrawIn(BaseModel):
    seed: int | None = None


@router.post("/calibrate")
async def draw(body: DrawIn, c: Conn) -> dict[str, int]:
    """Draw a session of about twenty papers. The seed is recorded with it."""
    seed = body.seed if body.seed is not None else secrets.randbelow(2**31)
    return {"id": await calibrate.draw(c, seed)}


@router.get("/calibrate")
async def sessions(c: Conn) -> list[dict[str, Any]]:
    return await calibrate.sessions(c)


# What the session endpoints answer besides success, for the API schema.
NO_SESSION = {404: {"description": "No such session"}}
NO_ITEM = {404: {"description": "No such session, or no paper at that position in it"}}


@router.get("/calibrate/{session_id}", responses=NO_SESSION)
async def session(session_id: int, c: Conn) -> dict[str, Any]:
    found = await calibrate.session(c, session_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such session")
    return found


@router.post("/calibrate/{session_id}/more", responses=NO_SESSION)
async def more(session_id: int, c: Conn) -> dict[str, int]:
    """Continue with the papers waiting for a word: splits, misses and thumbed papers."""
    if await calibrate.session(c, session_id) is None:
        raise HTTPException(status_code=404, detail="no such session")
    return {"added": await calibrate.more(c, session_id)}


@router.put("/calibrate/{session_id}/items/{position}", status_code=204, responses=NO_ITEM)
async def answer(session_id: int, position: int, body: calibrate.Answer, c: Conn) -> None:
    try:
        await calibrate.answer(c, session_id, position, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# -- runs and configuration ------------------------------------------------------------------------


@router.get("/runs")
async def runs(c: Conn) -> list[dict[str, Any]]:
    return await quality.runs(c)


class NextFire(BaseModel):
    kind: str
    cron: str
    next: datetime  # aware, in the schedule's zone


class ScheduleOut(BaseModel):
    """The schedule as the Runs page shows it: whether it is on, the zone the cron times are
    read in, and when each job next fires (or would, if it were on)."""

    enabled: bool
    zone: str
    jobs: list[NextFire]


@router.get("/schedule")
async def schedule(request: Request) -> ScheduleOut:
    cfg = request.app.state.config
    zone = cfg.schedule.zone
    now = datetime.now(zone)
    return ScheduleOut(
        enabled=cfg.schedule.enabled,
        zone=getattr(zone, "key", None) or str(zone),
        jobs=[
            NextFire(kind=job.kind, cron=job.cron, next=scheduler.next_fire(job.cron, now))
            for job in scheduler.jobs(cfg)
        ],
    )


@router.get("/config")
async def config(request: Request) -> dict[str, Any]:
    """The loaded configuration, secrets masked. Read-only: litwatch.toml is the source."""
    return _mask(request.app.state.config.model_dump(mode="json"))


def _mask(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _mask(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, key) for v in value]
    if value and _secret(key):
        return "••••"
    if isinstance(value, str) and (key == "url" or key.endswith("_url")):
        return _mask_url(value)
    return value


def _secret(key: str) -> bool:
    """Whether a setting's key names a credential: one of the secret words in it, unless the
    key names a limit (`max_output_tokens`, `max_*`), which the Configuration page shows."""
    k = key.lower()
    if k.startswith("max_") or k.endswith("_tokens"):
        return False
    return any(w in k for w in SECRET_WORDS)


def _mask_url(url: str) -> str:
    """A URL as far as its host: a webhook's path, query or user part may carry its token."""
    parts = urlsplit(url)
    if not parts.hostname:
        return "••••"
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    rest = parts.username or parts.password or parts.path.strip("/") or parts.query
    return f"{parts.scheme}://{host}" + ("/••••" if rest else "")
