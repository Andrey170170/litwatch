"""Settings, the kitchen behind the gear: topics, Quality, misses, runs and cost, and the
configuration (read-only, spec decision 11)."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError

from litwatch import store, watch
from litwatch.api.deps import Conn
from litwatch.core import Topic, TopicKind
from litwatch.feedback import calibrate, metrics, misses, replay, word
from litwatch.jobs import reruns
from litwatch.jobs.context import model_run
from litwatch.jobs.lock import RUN_LOCK, BusyError, run_lock
from litwatch.store import feedback as feedback_store
from litwatch.store import papers as papers_store
from litwatch.store import quality
from litwatch.store import runs as runs_store
from litwatch.store import topics as topics_store

router = APIRouter(prefix="/api/settings")
log = logging.getLogger(__name__)

SECRET_WORDS = ("key", "token", "password", "secret", "authorization", "credential")


def _errors(exc: ValidationError) -> list[Any]:
    """Validation errors as JSON: a custom validator's exception object is left out."""
    return exc.errors(include_url=False, include_context=False)


@asynccontextmanager
async def _logged(what: str) -> AsyncIterator[None]:
    """Background work logs its failure rather than raising into the server: the failure is
    already on its run record, where Settings shows it."""
    try:
        yield
    except Exception:
        log.exception("%s failed", what)


# -- topics ----------------------------------------------------------------------------------------


@router.get("/topics")
async def topics(c: Conn) -> list[Topic]:
    return await topics_store.all(c, None)


@router.put("/topics/{slug}")
async def save_topic(slug: str, body: dict[str, Any], c: Conn) -> Topic:
    """Create or edit a topic. Edits apply forward; a change of kind or content settles the
    topic's papers again (see feedback.word.settle_topic)."""
    try:
        topic = Topic.model_validate(body | {"slug": slug})
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_context=False)
        ) from exc
    async with c.transaction():
        before = await topics_store.kind_for_update(c, slug)
        revision = (await topics_store.get(c, slug) or topic).revision
        saved = await topics_store.save(c, topic)
        if before is not None and (before != topic.kind or saved.revision != revision):
            await word.settle_topic(c, slug)
    return saved


class Order(BaseModel):
    slugs: list[str]


@router.put("/topics-order", status_code=204)
async def order(body: Order, c: Conn) -> None:
    """The owner's order: sections, cards and the Papers table's take column follow it. The
    list must name every topic once. Rows are locked as a topic save locks them."""
    async with c.transaction():
        rows = await (
            await c.execute("SELECT slug FROM topics ORDER BY slug FOR UPDATE")
        ).fetchall()
        if sorted(body.slugs) != [r["slug"] for r in rows]:
            raise HTTPException(status_code=422, detail="name every topic exactly once")
        for position, slug in enumerate(body.slugs, 1):
            await c.execute("UPDATE topics SET position = %s WHERE slug = %s", (position, slug))


@router.get("/topics/{slug}/keywords")
async def keywords(
    slug: str, c: Conn, pattern: Annotated[list[str] | None, Query()] = None
) -> dict[str, int]:
    """How many stored papers each keyword pattern matches, as the keyword audit matches:
    the topic's saved patterns, or the `pattern`s given (an editor's unsaved draft). A
    pattern that is not a valid regular expression counts -1."""
    topic = await topics_store.get(c, slug)
    if topic is None and pattern is None:
        raise HTTPException(status_code=404, detail="no such topic")
    patterns = pattern if pattern is not None else topic.keywords
    valid = [p for p in patterns if _compiles(p)]
    return dict.fromkeys(patterns, -1) | await quality.keyword_counts(c, valid)


def _compiles(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


@router.post("/topics/{slug}/preview")
async def preview(
    slug: str, body: dict[str, Any], request: Request, c: Conn, background: BackgroundTasks
) -> dict[str, Any]:
    """Run a draft of the topic over its labelled papers and misses, as a dry run. Returns at
    once; the evaluation's metrics appear under /evaluations/{id} when it finishes."""
    try:
        draft = Topic.model_validate(body | {"slug": slug})
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_context=False)
        ) from exc
    estimate = await reruns.estimate(c, draft, "evidence")
    background.add_task(_preview, request.app.state, draft)
    return estimate


async def _preview(state: Any, draft: Topic) -> None:
    async with _logged("preview"), await store.connect(state.dsn) as conn:
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
    if body.scope == "range" and not (body.since and body.until):
        raise HTTPException(status_code=422, detail="a date range needs since and until")
    if body.scope != "evidence" and await _busy(c):
        raise HTTPException(status_code=409, detail="another litwatch job is running")
    estimate = await reruns.estimate(c, topic, body.scope, body.since, body.until)
    background.add_task(_rerun, request.app.state, slug, body)
    return estimate


async def _busy(c: Any) -> bool:
    """Whether a job holds the run lock now. A race with a job starting just after is caught
    when the re-run takes the lock, and recorded as a failed run."""
    row = await (await c.execute("SELECT pg_try_advisory_lock(%s) AS got", (RUN_LOCK,))).fetchone()
    if row["got"]:
        await c.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK,))
    return not row["got"]


async def _rerun(state: Any, slug: str, body: RerunIn) -> None:
    async with _logged("re-run"), await store.connect(state.dsn) as conn:
        try:
            await reruns.run(
                conn, state.config, slug, body.scope, body.since, body.until, gateway=state.gateway
            )
        except BusyError as exc:
            run_id = await runs_store.start(conn, f"rerun:{body.scope}")
            await runs_store.finish(conn, run_id, error=str(exc), counts={"topic": slug})


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
    if run["error"]:
        return {"eval_run": eval_run_id, "finished": True, "error": run["error"]}
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
        "calibration": await calibrate.precision(c),
        "matters_no_topic": await calibrate.matters_no_topic(c),
        "correction_rate": await quality.correction_rate(c),
        "collected": await quality.collected_reactions(c),
    }


class Pair(BaseModel):
    paper_id: str
    topic: str


@router.post("/quality/grade-now")
async def grade_now(body: Pair, request: Request, c: Conn) -> dict[str, Any]:
    """Grade an ungraded candidate now, under the run lock, and say what came of it."""
    state = request.app.state
    paper = await papers_store.get(c, body.paper_id)
    topic = await topics_store.get(c, body.topic)
    newest = {x.paper_id: x for x in await quality.newest_candidates(c, body.topic)}
    if paper is None or topic is None or body.paper_id not in newest:
        raise HTTPException(status_code=404, detail="no such candidate")
    if topic.kind != TopicKind.WATCH:
        raise HTTPException(status_code=409, detail="the topic is no longer a Watch topic")
    if newest[body.paper_id].state != "pending":
        raise HTTPException(status_code=409, detail="already graded")
    try:
        async with run_lock(c):
            # Read again under the lock: another request may have graded it meanwhile.
            again = {x.paper_id: x for x in await quality.newest_candidates(c, body.topic)}
            candidate = again.get(body.paper_id)
            if candidate is None or candidate.state != "pending":
                raise HTTPException(status_code=409, detail="already graded")
            async with model_run(c, state.config, "grade-now", gateway=state.gateway) as run:
                graded = await watch.grade(paper, topic, run.gateway)
                verdict = await watch.record(c, graded, candidate.topic_revision)
    except BusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"graded": verdict is not None, "error": graded.error}


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
    return await (
        await c.execute(
            "SELECT s.id, s.seed, s.drawn_at, count(i.*) AS papers, "
            "count(i.answered_at) AS answered FROM calibration_sessions s "
            "LEFT JOIN calibration_items i ON i.session_id = s.id "
            "GROUP BY s.id ORDER BY s.id DESC LIMIT 20"
        )
    ).fetchall()


@router.get("/calibrate/{session_id}")
async def session(session_id: int, c: Conn) -> dict[str, Any]:
    found = await calibrate.session(c, session_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such session")
    return found


@router.post("/calibrate/{session_id}/more")
async def more(session_id: int, c: Conn) -> dict[str, int]:
    """Continue with the papers waiting for a word: splits, misses and thumbed papers."""
    if await calibrate.session(c, session_id) is None:
        raise HTTPException(status_code=404, detail="no such session")
    return {"added": await calibrate.more(c, session_id)}


@router.put("/calibrate/{session_id}/items/{position}", status_code=204)
async def answer(session_id: int, position: int, body: calibrate.Answer, c: Conn) -> None:
    try:
        await calibrate.answer(c, session_id, position, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# -- misses, runs, configuration -------------------------------------------------------------------


@router.get("/misses")
async def reported(c: Conn) -> list[dict[str, Any]]:
    return await misses.reported(c)


@router.post("/misses/{miss_id}/trace", status_code=202)
async def retrace(miss_id: int, request: Request, background: BackgroundTasks) -> dict[str, str]:
    """Trace a miss again, after a failure. The owner's word on it is untouched."""
    background.add_task(_retrace, request.app.state, miss_id)
    return {"status": "tracing"}


async def _retrace(state: Any, miss_id: int) -> None:
    async with await store.connect(state.dsn) as conn:
        await misses.trace(conn, state.config, miss_id, gateway=state.gateway)


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
