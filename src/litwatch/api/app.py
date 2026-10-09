"""A small token-protected app: the labelling queues and the labels."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from importlib import resources
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from litwatch import store
from litwatch.core import Grade, Label, LabelOrigin, LabelValue, Status, TopicKind
from litwatch.store import feedback as feedback_store
from litwatch.store import topics as topics_store

COOKIE = "litwatch_token"
# The queue a label is given in says how its paper was chosen.
ORIGINS = {
    "rejects": LabelOrigin.REJECT_SAMPLE,
    "positives": LabelOrigin.LEGACY_POSITIVE,
    "unsure": LabelOrigin.INBOX,
}


class LabelIn(BaseModel):
    value: LabelValue
    grade: Grade | None = None
    note: str | None = None
    queue: str = "rejects"


async def _conn(request: Request) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await store.connect(request.app.state.dsn) as conn:
        yield conn


def _authorised(request: Request) -> None:
    given = request.cookies.get(COOKIE) or request.headers.get("authorization", "")[7:]
    if not hmac.compare_digest(given.encode(), request.app.state.token.encode()):
        raise HTTPException(status_code=401, detail="missing or wrong token")


Conn = Annotated[psycopg.AsyncConnection, Depends(_conn)]


def create_app(dsn: str, token: str) -> FastAPI:
    """The app over one database. Every request but the health check needs the shared token,
    as a bearer header or the cookie set by opening `/?token=...` once."""
    app = FastAPI(title="litwatch", docs_url=None, redoc_url=None)
    app.state.dsn, app.state.token = dsn, token
    protected = [Depends(_authorised)]

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def page(request: Request, response: Response) -> str:
        given = request.query_params.get("token")
        if given and hmac.compare_digest(given.encode(), token.encode()):
            response.set_cookie(COOKIE, given, httponly=True, samesite="strict", max_age=90 * 86400)
        return resources.files("litwatch.api").joinpath("static/label.html").read_text()

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

    @app.put("/api/labels/{paper_id}/{topic}", dependencies=protected)
    async def put_label(paper_id: str, topic: str, body: LabelIn, c: Conn) -> Label:
        if topic not in {t["slug"] for t in await watched(c)}:
            raise HTTPException(status_code=404, detail="not an active Watch topic")
        try:
            return await feedback_store.set_label(
                c,
                Label(
                    paper_id=paper_id,
                    topic=topic,
                    value=body.value,
                    grade=body.grade if body.value == LabelValue.RELEVANT else None,
                    origin=ORIGINS.get(body.queue, LabelOrigin.INBOX),
                    note=body.note,
                ),
            )
        except psycopg.errors.ForeignKeyViolation as exc:
            raise HTTPException(status_code=404, detail="no such paper") from exc

    @app.delete("/api/labels/{paper_id}/{topic}", dependencies=protected, status_code=204)
    async def delete_label(paper_id: str, topic: str, c: Conn) -> None:
        await feedback_store.delete_label(c, paper_id, topic)

    return app
