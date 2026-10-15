"""Settings, the kitchen behind the gear: the taxonomy and its topics, Quality, Calibrate,
runs, and the configuration (read-only, spec decision 11)."""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from litwatch.api.deps import Conn
from litwatch.core import Taxonomy, Topic
from litwatch.feedback import calibrate, word
from litwatch.jobs import scheduler
from litwatch.store import quality
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
        },
        "calibration": await calibrate.precision(c),
        "matters_no_topic": await calibrate.matters_no_topic(c),
        "collected": await quality.collected_reactions(c),
    }


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
