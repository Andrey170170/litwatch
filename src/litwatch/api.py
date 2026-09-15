"""Internal HTTP API for n8n: sync selections, start a run, read runs and member papers. No
auth: it is reachable only on the stack network and never published on a host port."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError

from litwatch import db, ingest
from litwatch.config import Settings
from litwatch.deep import DeepClient, DeepProcessor
from litwatch.digest import Digester
from litwatch.route import Router
from litwatch.screen import Cascade, ModelScreener, openrouter_client
from litwatch.selection import Selection, Status, from_spec
from litwatch.webdav import WebDAVWriter
from litwatch.zotero import ZoteroClient

log = logging.getLogger(__name__)


class RunRequest(BaseModel):
    days: int = Field(default=3, ge=1, le=60)
    kind: str = Field(default="daily", pattern="^(daily|backfill|manual)$")
    fetch: bool = True


class SelectionRow(BaseModel):
    """One Notion row as n8n sends it. Validation errors go back per row, never as a 4xx."""

    notion_id: str | None = None
    slug: str | None = None
    name: str
    owner: str | None = None
    status: str = "active"
    subject: str = ""
    rubric: str | None = None
    keywords: list[str] | str | None = None
    digest: bool = False


class SelectionsSync(BaseModel):
    selections: list[SelectionRow]
    # An authoritative sync (the Notion data source) pauses selections it does not mention.
    authoritative: bool = True


def build_cascade(
    settings: Settings, selections: list[Selection], rescreen: bool = True, confirm: bool = True
) -> Cascade:
    client = openrouter_client(settings.openrouter_api_key)
    return Cascade(
        selections,
        ModelScreener(
            client,
            settings.screen_model,
            settings.screen_effort,
            provider_order=(
                settings.screen_provider_order
                if settings.screen_model == "openai/gpt-5.6-luna"
                else ()
            ),
            timeout_seconds=settings.screen_timeout_seconds,
        ),
        ModelScreener(
            client,
            settings.rescreen_model,
            settings.rescreen_effort,
            max_completion_tokens=settings.rescreen_max_completion_tokens,
            operation="rescreen",
        )
        if rescreen
        else None,
        ModelScreener(client, settings.confirm_model, settings.confirm_effort, operation="confirm")
        if confirm
        else None,
        rescreen_ignored_keywords=settings.rescreen_ignored_keywords,
    )


def build_router(settings: Settings, attach_pdf: bool = True) -> Router | None:
    """None when Zotero is not configured: runs then screen only, routing waits."""
    if not settings.routing_enabled:
        return None
    assert settings.zotero_user_id and settings.zotero_api_key
    webdav = None
    if settings.webdav_enabled:
        assert settings.webdav_url and settings.webdav_username and settings.webdav_password
        webdav = WebDAVWriter(
            settings.webdav_url, settings.webdav_username, settings.webdav_password
        )
    return Router(
        ZoteroClient(settings.zotero_user_id, settings.zotero_api_key),
        webdav,
        attach_pdf=attach_pdf,
    )


def build_digester(settings: Settings, model: str | None = None) -> Digester:
    client = openrouter_client(settings.openrouter_api_key)
    return Digester(client, model or settings.digest_model, settings.digest_effort)


def build_deep(settings: Settings, selections: list[Selection]) -> DeepProcessor | None:
    """None when litwatch-deep is not configured. Zotero write-back needs routing too."""
    if not settings.deep_enabled:
        return None
    assert settings.deep_url
    router = build_router(settings)
    return DeepProcessor(
        DeepClient(settings.deep_url),
        router.zotero if router else None,
        selections,
        extensions=settings.deep_extensions,
        inbox_collection=settings.inbox_collection,
        inbox_selections=settings.inbox_selections,
    )


async def active_selections(conn: psycopg.AsyncConnection) -> list[Selection]:
    return await db.list_selections(conn, Status.ACTIVE)


async def sync_selections(
    conn: psycopg.AsyncConnection, sync: SelectionsSync
) -> list[dict[str, Any]]:
    """Validate and upsert each row; report per row. Missing rows are paused when authoritative."""
    report, kept = [], []
    counts = await db.selection_counts(conn)
    for row in sync.selections:
        try:
            spec = from_spec(row.model_dump())
        except (ValidationError, ValueError) as exc:
            report.append(
                {"notion_id": row.notion_id, "name": row.name, "ok": False, "error": _brief(exc)}
            )
            continue
        changed = await db.upsert_selection(conn, spec)
        kept.append(spec.slug)
        tiers = counts.get(spec.slug, {})
        report.append(
            {
                "notion_id": spec.notion_id,
                "name": spec.name,
                "slug": spec.slug,
                "ok": True,
                "version": spec.version,
                "changed": changed,
                "status": spec.status.value,
                "tier_1": tiers.get(1, 0),
                "tier_2": tiers.get(2, 0),
                "tier_3": tiers.get(3, 0),
            }
        )
    if sync.authoritative:
        for slug in await db.pause_missing_selections(conn, kept):
            log.info("selection %s paused: absent from the sync", slug)
    # A row set to draft or paused in Notion stops counting toward its members' best tier.
    inactive = [r["slug"] for r in report if r.get("ok") and r["status"] != Status.ACTIVE.value]
    if inactive:
        await db.recompute_best_tier(conn, inactive)
    return report


def _brief(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return str(exc)


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
                selections = await active_selections(conn)
                state["last"] = await ingest.run(
                    conn,
                    settings,
                    build_cascade(settings, selections),
                    req.kind,
                    req.days,
                    req.fetch,
                    router=build_router(settings),
                    digester=build_digester(settings),
                    deep=build_deep(settings, selections),
                )
        except Exception:  # noqa: BLE001 - already recorded on the run row
            log.exception("background run failed")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        async with await db.connect(dsn) as conn:
            return {"ok": True, **await db.stats(conn)}

    @app.get("/selections")
    async def selections() -> list[dict[str, Any]]:
        async with await db.connect(dsn) as conn:
            rows, counts = await db.list_selections(conn), await db.selection_counts(conn)
        return [
            {**s.model_dump(), "version": s.version, "counts": counts.get(s.slug, {})} for s in rows
        ]

    @app.put("/selections")
    async def put_selections(sync: SelectionsSync) -> list[dict[str, Any]]:
        async with await db.connect(dsn) as conn:
            return await sync_selections(conn, sync)

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

    @app.get("/digests")
    async def digests(limit: int = 40) -> list[dict[str, Any]]:
        async with await db.connect(dsn) as conn:
            return await db.list_digests(conn, limit)

    @app.get("/papers")
    async def papers(
        selection: str | None = None,
        tier: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Member papers. `tier` is a comma-separated list such as `1,2`; with `selection` it
        applies to that selection's verdict, otherwise to the paper's best tier."""
        try:
            tiers = [int(t) for t in (tier or "").split(",") if t] or None
        except ValueError as exc:
            raise HTTPException(422, "tier must be integers 1..3") from exc
        async with await db.connect(dsn) as conn:
            return await db.list_papers(conn, selection, tiers, since, min(limit, 5000))

    @app.get("/papers/search")
    async def search(q: str, limit: int = 10) -> list[dict[str, Any]]:
        """Full-text search for the deep processor's `store.search`."""
        async with await db.connect(dsn) as conn:
            return await db.search_papers(conn, q, limit)

    @app.get("/papers/{paper_id}")
    async def paper(paper_id: str) -> dict[str, Any]:
        async with await db.connect(dsn) as conn:
            row = await db.paper_summary(conn, paper_id)
        if row is None:
            raise HTTPException(404, "no such paper")
        return row

    @app.get("/papers/{paper_id}/trace")
    async def trace(paper_id: str) -> list[dict[str, Any]]:
        async with await db.connect(dsn) as conn:
            row = await db.get_paper(conn, paper_id)
        if row is None:
            raise HTTPException(404, "no such paper")
        return row.get("deep_trace") or []

    return app
