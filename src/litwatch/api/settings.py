"""Settings, the kitchen behind the gear: topics, Quality, Calibrate, runs, and the
configuration (read-only, spec decision 11)."""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from litwatch.api.deps import Conn
from litwatch.core import Topic
from litwatch.feedback import calibrate, word
from litwatch.jobs import scheduler
from litwatch.store import quality
from litwatch.store import topics as topics_store

router = APIRouter(prefix="/api/settings")

SECRET_WORDS = ("key", "token", "password", "secret", "authorization", "credential")


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
    """The owner's order, which topic lists and cards follow. The list must name every topic
    once. Rows are locked as a topic save locks them."""
    async with c.transaction():
        rows = await (
            await c.execute("SELECT slug FROM topics ORDER BY slug FOR UPDATE")
        ).fetchall()
        if sorted(body.slugs) != [r["slug"] for r in rows]:
            raise HTTPException(status_code=422, detail="name every topic exactly once")
        for position, slug in enumerate(body.slugs, 1):
            await c.execute("UPDATE topics SET position = %s WHERE slug = %s", (position, slug))


# -- quality ---------------------------------------------------------------------------------------


@router.get("/quality")
async def quality_page(c: Conn) -> dict[str, Any]:
    """At the top, what last night left; below, how well the Watch topics are doing."""
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
