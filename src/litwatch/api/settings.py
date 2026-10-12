"""Settings, the kitchen behind the gear: topics, Quality, misses, runs and cost, and the
configuration (read-only, spec decision 11)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ValidationError

from litwatch import store, watch
from litwatch.api.deps import Conn
from litwatch.core import Topic, TopicKind
from litwatch.feedback import metrics, misses, replay, word
from litwatch.jobs import reruns
from litwatch.jobs.context import model_run
from litwatch.store import feedback as feedback_store
from litwatch.store import papers as papers_store
from litwatch.store import quality
from litwatch.store import topics as topics_store

router = APIRouter(prefix="/api/settings")

SECRET_WORDS = ("key", "token", "password", "secret")


# -- topics ----------------------------------------------------------------------------------------


@router.get("/topics")
async def topics(c: Conn) -> list[Topic]:
    return await topics_store.all(c, None)


@router.put("/topics/{slug}")
async def save_topic(slug: str, body: dict[str, Any], c: Conn) -> Topic:
    """Create or edit a topic. Edits apply forward; a change of kind settles the topic's
    papers again, so a topic that stops being Watch takes its grades with it."""
    try:
        topic = Topic.model_validate(body | {"slug": slug})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    async with c.transaction():
        before = await topics_store.kind_for_update(c, slug)
        saved = await topics_store.save(c, topic)
        if before is not None and before != topic.kind:
            await word.settle_topic(c, slug)
    return saved


class Order(BaseModel):
    slugs: list[str]


@router.put("/topics-order", status_code=204)
async def order(body: Order, c: Conn) -> None:
    """The owner's order: sections, cards and the Papers table's take column follow it."""
    async with c.transaction():
        for position, slug in enumerate(body.slugs, 1):
            await c.execute("UPDATE topics SET position = %s WHERE slug = %s", (position, slug))


@router.get("/topics/{slug}/keywords")
async def keywords(slug: str, c: Conn) -> dict[str, int]:
    """How many stored papers each keyword pattern matches; -1 for one Postgres rejects."""
    topic = await topics_store.get(c, slug)
    if topic is None:
        raise HTTPException(status_code=404, detail="no such topic")
    return await quality.keyword_counts(c, topic.keywords)


@router.post("/topics/{slug}/preview")
async def preview(
    slug: str, body: dict[str, Any], request: Request, c: Conn, background: BackgroundTasks
) -> dict[str, Any]:
    """Run a draft of the topic over its labelled papers and misses, as a dry run. Returns at
    once; the evaluation's metrics appear under /evaluations/{id} when it finishes."""
    try:
        draft = Topic.model_validate(body | {"slug": slug})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    estimate = await reruns.estimate(c, draft, "evidence")
    background.add_task(_preview, request.app.state, draft)
    return estimate


async def _preview(state: Any, draft: Topic) -> None:
    async with await store.connect(state.dsn) as conn:
        ids = await quality.evidence_papers(conn, draft.slug)
        papers = await papers_store.get_many(conn, ids)
        await replay(
            conn,
            state.config,
            papers,
            kind="preview",
            params={"topic": draft.slug},
            gateway=state.gateway,
            draft=draft,
        )


@router.get("/topics/{slug}/reruns")
async def rerun_estimates(
    slug: str, c: Conn, since: date | None = None, until: date | None = None
) -> list[dict[str, Any]]:
    topic = await topics_store.get(c, slug)
    if topic is None or topic.kind != TopicKind.WATCH:
        raise HTTPException(status_code=404, detail="not a Watch topic")
    scopes = [s for s in reruns.SCOPES if s != "range" or (since and until)]
    return [await reruns.estimate(c, topic, s, since, until) for s in scopes]


class RerunIn(BaseModel):
    scope: reruns.Scope
    since: date | None = None
    until: date | None = None


@router.post("/topics/{slug}/reruns", status_code=202)
async def rerun(
    slug: str, body: RerunIn, request: Request, c: Conn, background: BackgroundTasks
) -> dict[str, Any]:
    topic = await topics_store.get(c, slug)
    if topic is None or topic.kind != TopicKind.WATCH:
        raise HTTPException(status_code=404, detail="not a Watch topic")
    estimate = await reruns.estimate(c, topic, body.scope, body.since, body.until)
    background.add_task(_rerun, request.app.state, slug, body)
    return estimate


async def _rerun(state: Any, slug: str, body: RerunIn) -> None:
    async with await store.connect(state.dsn) as conn:
        await reruns.run(
            conn, state.config, slug, body.scope, body.since, body.until, gateway=state.gateway
        )


@router.get("/evaluations")
async def evaluations(c: Conn, kind: str | None = None) -> list[dict[str, Any]]:
    return await (
        await c.execute(
            "SELECT e.id, e.kind, e.note, e.params, e.started_at, e.finished_at, r.cost "
            "FROM eval_runs e LEFT JOIN runs r ON r.id = e.run_id "
            "WHERE %s::text IS NULL OR e.kind = %s ORDER BY e.id DESC LIMIT 50",
            (kind, kind),
        )
    ).fetchall()


@router.get("/evaluations/{eval_run_id}")
async def evaluation(eval_run_id: int, c: Conn) -> dict[str, Any]:
    run = await feedback_store.get_eval(c, eval_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such evaluation")
    if run["finished_at"] is None:
        return {"eval_run": eval_run_id, "finished": False}
    return {"finished": True, **await metrics(c, eval_run_id)}


# -- quality ---------------------------------------------------------------------------------------


@router.get("/quality")
async def quality_page(request: Request, c: Conn) -> dict[str, Any]:
    """At the top, what last night left; below, how well the Watch topics are doing."""
    limits = request.app.state.config.limits
    night = await quality.last_night(c)
    since = night["started_at"] if night else date.today() - timedelta(days=1)
    latest_replay = await feedback_store.latest_eval(c, "replay")
    return {
        "night": {
            "run": night["id"] if night else None,
            "error": night["error"] if night else None,
            "splits": await quality.splits_since(c, since),
            "ungraded": await quality.ungraded(c, limits.grading_attempts),
            "awaiting_triage": await quality.awaiting_triage(c, limits.triage_stuck_after),
        },
        "evaluation": await metrics(c, latest_replay["id"]) if latest_replay else None,
        "correction_rate": await quality.correction_rate(c),
        "collected": await quality.collected_reactions(c),
    }


class Pair(BaseModel):
    paper_id: str
    topic: str


@router.post("/quality/grade-now", status_code=202)
async def grade_now(body: Pair, request: Request, background: BackgroundTasks) -> dict[str, str]:
    """Grade an ungraded candidate now, outside the nightly run."""
    background.add_task(_grade_now, request.app.state, body)
    return {"status": "grading"}


async def _grade_now(state: Any, pair: Pair) -> None:
    async with await store.connect(state.dsn) as conn:
        paper = await papers_store.get(conn, pair.paper_id)
        topic = await topics_store.get(conn, pair.topic)
        if paper is None or topic is None:
            return
        async with model_run(conn, state.config, "grade-now", gateway=state.gateway) as run:
            graded = await watch.grade(paper, topic, run.gateway)
            await watch.record(conn, graded)


# -- misses, runs, configuration -------------------------------------------------------------------


@router.get("/misses")
async def reported(c: Conn) -> list[dict[str, Any]]:
    return await misses.reported(c)


@router.get("/runs")
async def runs(c: Conn) -> list[dict[str, Any]]:
    return await quality.runs(c)


@router.get("/config")
async def config(request: Request) -> dict[str, Any]:
    """The loaded configuration, secrets masked. Read-only: litwatch.toml is the source."""
    return _mask(request.app.state.config.model_dump(mode="json"))


def _mask(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _mask(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, key) for v in value]
    if value and any(w in key.lower() for w in SECRET_WORDS):
        return "••••"
    return value
