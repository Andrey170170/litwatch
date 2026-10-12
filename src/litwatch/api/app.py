"""The service's HTTP side: the JSON API the web app reads, and the evaluation's labelling page."""

from __future__ import annotations

from importlib import resources
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from litwatch.api import reading
from litwatch.api.deps import COOKIE, Conn, authorised, token_ok
from litwatch.config import Config
from litwatch.core import Grade, Label, LabelOrigin, LabelValue, Status, TopicKind
from litwatch.feedback import word
from litwatch.jobs.context import GatewayFactory, default_gateway
from litwatch.store import feedback as feedback_store
from litwatch.store import topics as topics_store

# The queue a label is given in says how its paper was chosen.
ORIGINS = {
    "rejects": LabelOrigin.REJECT_SAMPLE,
    "positives": LabelOrigin.LEGACY_POSITIVE,
    "unsure": LabelOrigin.READER,
    "adjacent": LabelOrigin.READER,
}


class LabelIn(BaseModel):
    value: LabelValue
    grade: Grade | None = None
    note: str | None = None
    queue: str = "rejects"


def create_app(
    dsn: str,
    token: str,
    config: Config | None = None,
    gateway: GatewayFactory = default_gateway,
) -> FastAPI:
    """The app over one database. Every request but the health check and signing in needs
    the shared token, as a bearer header or the cookie (set by `/api/session`, or by opening
    the labelling page once with `?token=`)."""
    app = FastAPI(title="litwatch", docs_url=None, redoc_url=None)
    app.state.dsn, app.state.token = dsn, token
    app.state.config, app.state.gateway = config or Config(), gateway
    protected = [Depends(authorised)]
    app.include_router(reading.router, dependencies=protected)
    app.include_router(reading.session)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # -- the evaluation's labelling page (replaced by Calibrate later in M3) ----------------------

    @app.get("/label", response_class=HTMLResponse)
    async def page(request: Request) -> Response:
        given = request.query_params.get("token")
        if given and token_ok(request, given):
            # Straight on to the bare address, so the token does not stay in the address bar,
            # the history or a later referrer.
            response = RedirectResponse("/label", status_code=303)
            response.set_cookie(COOKIE, given, httponly=True, samesite="strict", max_age=90 * 86400)
            return response
        return HTMLResponse(
            resources.files("litwatch.api").joinpath("static/label.html").read_text()
        )

    async def watched(c: psycopg.AsyncConnection) -> list[dict[str, str]]:
        topics = await topics_store.all(c, Status.ACTIVE)
        return [{"slug": t.slug, "name": t.name} for t in topics if t.kind == TopicKind.WATCH]

    @app.get("/api/labelling", dependencies=protected)
    async def labelling(c: Conn) -> dict[str, Any]:
        topics = await watched(c)
        sets = await feedback_store.labelling_sets(c, [t["slug"] for t in topics])
        return {"topics": topics, "sets": sets}

    @app.get("/api/labelling/{key}", dependencies=protected)
    async def queue(key: str, c: Conn) -> list[dict[str, Any]]:
        if key not in ORIGINS:
            raise HTTPException(status_code=404, detail="no such queue")
        return await feedback_store.labelling_queue(c, key)

    # The paper id comes last and may contain a slash (`cs/9901001`).
    @app.put("/api/labels/{topic}/{paper_id:path}", dependencies=protected)
    async def put_label(topic: str, paper_id: str, body: LabelIn, c: Conn) -> Label:
        if topic not in {t["slug"] for t in await watched(c)}:
            raise HTTPException(status_code=404, detail="not an active Watch topic")
        try:
            stored, _ = await word.label(
                c,
                Label(
                    paper_id=paper_id,
                    topic=topic,
                    value=body.value,
                    grade=body.grade if body.value == LabelValue.RELEVANT else None,
                    origin=ORIGINS.get(body.queue, LabelOrigin.READER),
                    note=body.note,
                ),
            )
            return stored
        except psycopg.errors.ForeignKeyViolation as exc:
            raise HTTPException(status_code=404, detail="no such paper") from exc

    @app.delete("/api/labels/{topic}/{paper_id:path}", dependencies=protected, status_code=204)
    async def delete_label(topic: str, paper_id: str, c: Conn) -> None:
        await word.unlabel(c, paper_id, topic)

    return app
