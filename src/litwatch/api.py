"""Internal HTTP API for n8n: start a run, read runs and screened papers. No auth: it is
reachable only on the stack network and never published on a host port."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from litwatch import db, ingest
from litwatch.config import Settings
from litwatch.screen import Cascade, ModelScreener, openrouter_client

log = logging.getLogger(__name__)


class RunRequest(BaseModel):
    days: int = Field(default=3, ge=1, le=60)
    kind: str = Field(default="daily", pattern="^(daily|backfill|manual)$")
    fetch: bool = True


def build_cascade(settings: Settings) -> Cascade:
    client = openrouter_client(settings.openrouter_api_key)
    return Cascade(
        ModelScreener(client, settings.screen_model, settings.screen_effort),
        ModelScreener(client, settings.rescreen_model, settings.rescreen_effort),
    )


def create_app(settings: Settings) -> FastAPI:
    dsn = settings.require_database()
    state: dict[str, Any] = {"task": None, "last": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with await db.connect(dsn) as conn:
            await db.apply_schema(conn)
        yield

    app = FastAPI(title="litwatch", lifespan=lifespan)
    app.state.runs = state  # exposes the in-flight task, for tests and diagnostics

    async def execute(req: RunRequest) -> None:
        try:
            async with await db.connect(dsn) as conn:
                state["last"] = await ingest.run(
                    conn, settings, build_cascade(settings), req.kind, req.days, req.fetch
                )
        except Exception:  # noqa: BLE001 - already recorded on the run row
            log.exception("background run failed")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        async with await db.connect(dsn) as conn:
            return {"ok": True, **await db.stats(conn)}

    @app.post("/runs", status_code=202)
    async def start_run(req: RunRequest) -> dict[str, Any]:
        task = state["task"]
        if task is not None and not task.done():
            raise HTTPException(409, "a run is already in progress")
        state["task"] = asyncio.create_task(execute(req))
        return {"started": True, "days": req.days, "kind": req.kind}

    @app.get("/runs")
    async def runs(limit: int = 20) -> list[dict[str, Any]]:
        async with await db.connect(dsn) as conn:
            return await db.list_runs(conn, limit)

    @app.get("/runs/{run_id}")
    async def run(run_id: int) -> dict[str, Any]:
        async with await db.connect(dsn) as conn:
            row = await db.get_run(conn, run_id)
        if row is None:
            raise HTTPException(404, "no such run")
        return row

    @app.get("/papers")
    async def papers(
        tier: str | None = None, since: datetime | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with await db.connect(dsn) as conn:
            return await db.list_papers(conn, tier, since, min(limit, 1000))

    return app
