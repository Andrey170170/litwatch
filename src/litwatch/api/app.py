"""A small token-protected app: the labelling queues and the labels."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from importlib import resources
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
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
    "adjacent": LabelOrigin.INBOX,
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
    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    given = [request.cookies.get(COOKIE), bearer if scheme.lower() == "bearer" else None]
    token = request.app.state.token.encode()
    # Either credential is enough, so a stale cookie does not block a good header.
    if not any(g and hmac.compare_digest(g.encode(), token) for g in given):
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
    async def page(request: Request) -> Response:
        given = request.query_params.get("token")
        if given and hmac.compare_digest(given.encode(), token.encode()):
            # Straight on to the bare address, so the token does not stay in the address bar,
            # the history or a later referrer.
            response = RedirectResponse("/", status_code=303)
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

    @app.delete("/api/labels/{topic}/{paper_id:path}", dependencies=protected, status_code=204)
    async def delete_label(topic: str, paper_id: str, c: Conn) -> None:
        await feedback_store.delete_label(c, paper_id, topic)

    return app
