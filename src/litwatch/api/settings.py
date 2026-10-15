"""Settings, the kitchen behind the gear: the taxonomy and its topics, Quality and the papers
awaiting classification, Calibrate, runs, and the configuration (read-only, spec decision 11)."""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from litwatch import classification
from litwatch.api.deps import Conn
from litwatch.core import FAILED_RUNS, Paper, Taxonomy, Topic
from litwatch.feedback import calibrate, word
from litwatch.jobs import scheduler
from litwatch.store import failures, quality
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store

router = APIRouter(prefix="/api/settings")

SECRET_WORDS = ("key", "token", "password", "secret", "authorization", "credential")


# -- the taxonomy ----------------------------------------------------------------------------------


@router.get("/taxonomy")
async def taxonomy(c: Conn) -> Taxonomy | None:
    """The class tree and the followed topics; null before one is imported."""
    return await taxonomy_store.load(c)


@router.put("/topics/{slug}")
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
        await taxonomy_store.set_order(c, body.slugs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# -- quality ---------------------------------------------------------------------------------------


@router.get("/quality")
async def quality_page(c: Conn) -> dict[str, Any]:
    """At the top, what last night left; below, how well the Focus topics are doing."""
    night = await quality.last_night(c)
    since = night["started_at"] if night else date.today() - timedelta(days=1)
    return {
        "night": {
            "run": night["id"] if night else None,
            "error": night["error"] if night else None,
            "splits": await quality.splits_since(c, since),
            # Classification failed three runs in a row: these wait for the owner.
            "awaiting_classification": await failures.stuck(c, "classification", FAILED_RUNS),
            # Analysis failed three runs in a row; it is tried again every run.
            "analysis_failed": await failures.stuck(c, "analysis", FAILED_RUNS),
        },
        "calibration": await calibrate.precision(c),
        "matters_no_topic": await calibrate.matters_no_topic(c),
        "collected": await quality.collected_reactions(c),
    }


# -- papers classification kept failing on (pipeline.md, section 9) -------------------------------


async def _awaiting(c: Any, paper_id: str) -> Paper:
    waiting = {r["paper_id"] for r in await failures.stuck(c, "classification", FAILED_RUNS)}
    paper = await papers_store.get(c, paper_id) if paper_id in waiting else None
    if paper is None:
        raise HTTPException(status_code=404, detail="not a paper awaiting classification")
    return paper


@router.post("/classification/{paper_id:path}/dismiss", status_code=204)
async def dismiss(paper_id: str, c: Conn) -> None:
    """Litwatch drops the paper from processing; it stays searchable in the archive."""
    await _awaiting(c, paper_id)
    await papers_store.dismiss(c, paper_id)


class ClassesIn(BaseModel):
    classes: list[str]  # class slugs, the primary one first unless `primary` names it
    primary: str | None = None


@router.put("/classification/{paper_id:path}")
async def classify_by_hand(paper_id: str, body: ClassesIn, c: Conn) -> dict[str, Any]:
    """The owner names the paper's classes, which go on as usual with source `owner`."""
    paper = await _awaiting(c, paper_id)
    tree = await taxonomy_store.load(c)
    if tree is None:
        raise HTTPException(status_code=409, detail="no taxonomy")
    try:
        recorded = await classification.by_hand(c, paper, tree, body.classes, body.primary)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not recorded:
        raise HTTPException(status_code=409, detail="the paper changed meanwhile")
    return {
        "classes": list(dict.fromkeys(body.classes)),
        "primary": body.primary or body.classes[0],
    }


class CorrectionIn(BaseModel):
    title: str | None = None
    abstract: str | None = None


@router.post("/classification/{paper_id:path}/correction", status_code=204)
async def correct(paper_id: str, body: CorrectionIn, c: Conn) -> None:
    """Fix the text classification choked on. It counts as a rewrite: the next run classifies
    the corrected paper."""
    await _awaiting(c, paper_id)
    title = (body.title or "").strip() or None
    abstract = (body.abstract or "").strip() or None
    if not (title or abstract):
        raise HTTPException(status_code=422, detail="give a title or an abstract")
    await papers_store.correct(c, paper_id, title, abstract)


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
    if value and any(w in key.lower() for w in SECRET_WORDS):
        return "••••"
    return value
