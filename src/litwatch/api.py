"""Internal HTTP API for n8n: sync selections, start a run, read runs and member papers. No
auth: it is reachable only on the stack network and never published on a host port."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError

from litwatch import archive_delivery, archive_outbox, db, ingest
from litwatch.archive_client import ArchiveClient
from litwatch.config import Settings
from litwatch.deep import DeepClient, DeepProcessor
from litwatch.digest import Digester
from litwatch.route import Router
from litwatch.screen import Cascade, ModelScreener, openrouter_client
from litwatch.selection import Selection, Status, from_spec
from litwatch.text import BodyFetcher
from litwatch.webdav import WebDAVWriter
from litwatch.zotero import ZoteroClient

log = logging.getLogger(__name__)


class RunRequest(BaseModel):
    days: int = Field(default=3, ge=1, le=60)
    kind: str = Field(default="daily", pattern="^(daily|backfill|manual)$")
    fetch: bool = True


class ProjectionCursor(BaseModel):
    """A position in the changed-members feed, as `GET /projection/changes` hands it out."""

    changed_at: datetime
    paper_id: str = ""


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
            provider_order=settings.providers_for(settings.screen_model),
            timeout_seconds=settings.screen_timeout_seconds,
        ),
        ModelScreener(
            client,
            settings.rescreen_model,
            settings.rescreen_effort,
            max_completion_tokens=settings.rescreen_max_completion_tokens,
            provider_order=settings.providers_for(settings.rescreen_model),
            operation="rescreen",
        )
        if rescreen
        else None,
        ModelScreener(
            client,
            settings.confirm_model,
            settings.confirm_effort,
            provider_order=settings.providers_for(settings.confirm_model),
            operation="confirm",
        )
        if confirm
        else None,
        rescreen_ignored_keywords=settings.rescreen_ignored_keywords,
    )


def build_archive(settings: Settings) -> ArchiveClient | None:
    if not settings.archive_enabled:
        return None
    assert settings.archive_url and settings.archive_token
    return ArchiveClient(
        settings.archive_url, settings.archive_token, wait_seconds=settings.archive_wait_seconds
    )


def build_router(
    settings: Settings, attach_pdf: bool = True, *, use_archive: bool = True
) -> Router | None:
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
        archive=build_archive(settings) if use_archive else None,
    )


def build_digester(settings: Settings, model: str | None = None) -> Digester:
    client = openrouter_client(settings.openrouter_api_key)
    model = model or settings.digest_model
    return Digester(client, model, settings.digest_effort, settings.providers_for(model))


def build_deep(settings: Settings, selections: list[Selection]) -> DeepProcessor | None:
    """None when litwatch-deep is not configured. Zotero write-back needs routing too."""
    if not settings.deep_enabled:
        return None
    assert settings.deep_url
    router = build_router(settings, use_archive=False)
    return DeepProcessor(
        DeepClient(settings.deep_url),
        router.zotero if router else None,
        selections,
        extensions=settings.deep_extensions,
        inbox_collection=settings.inbox_collection,
        inbox_selections=settings.inbox_selections,
        fetcher=BodyFetcher(archive=build_archive(settings)),
    )


async def active_selections(conn: psycopg.AsyncConnection) -> list[Selection]:
    return await db.list_selections(conn, Status.ACTIVE)


async def sync_selections(
    conn: psycopg.AsyncConnection, sync: SelectionsSync
) -> list[dict[str, Any]]:
    """Validate and upsert each row; report per row. Missing rows are paused when authoritative.

    One transaction: a status that changed without its members' best tier recomputed would
    never be recomputed by a later sync, which sees no change.
    """
    async with conn.transaction():
        return await _sync_selections(conn, sync)


async def _sync_selections(
    conn: psycopg.AsyncConnection, sync: SelectionsSync
) -> list[dict[str, Any]]:
    report, kept = [], []
    counts = await db.selection_counts(conn)
    was_active = {s.slug for s in await db.list_selections(conn, Status.ACTIVE)}
    known = {s.slug for s in await db.list_selections(conn)}
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
    # A row set to draft or paused in Notion stops counting toward its members' best tier, and
    # one switched back to active counts again: its kept verdicts are not screened anew.
    moved = [
        r["slug"]
        for r in report
        if r.get("ok")
        and (
            r["status"] != Status.ACTIVE.value
            or (r["slug"] in known and r["slug"] not in was_active)
        )
    ]
    if moved:
        await db.recompute_best_tier(conn, moved)
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
        client = build_archive(settings)
        stop = asyncio.Event()
        delivery = None
        if client is not None:
            delivery = asyncio.create_task(
                archive_delivery.run_dispatcher(
                    dsn, client, stop, interval_seconds=settings.archive_dispatch_seconds
                )
            )
        drain = asyncio.create_task(backlog_worker(stop)) if settings.backlog_enabled else None
        try:
            yield
        finally:
            stop.set()
            for task in (delivery, drain):
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            if client is not None:
                await client.aclose()

    app = FastAPI(title="litwatch", lifespan=lifespan)
    app.state.runs = state  # exposes the in-flight task, for tests and diagnostics

    async def backlog_worker(stop: asyncio.Event) -> None:
        """Between runs, re-screen papers judged under an older selection version."""
        failed: set[str] = set()  # not retried until the service restarts
        try:
            async with await db.connect(dsn) as conn:
                await db.close_interrupted_backlog(conn)
        except Exception:  # noqa: BLE001 - the loop below reports a database that stays down
            log.exception("backlog: could not close interrupted drains")
        while not stop.is_set():
            router = None
            try:
                async with await db.connect(dsn) as conn:
                    selections = await active_selections(conn)
                    if selections:
                        router = build_router(settings)
                        await ingest.drain_backlog(
                            conn,
                            settings,
                            build_cascade(settings, selections),
                            router,
                            stop,
                            failed,
                        )
            except Exception:  # noqa: BLE001 - recorded on the run row; try again next round
                log.exception("backlog drain failed")
            finally:
                if getattr(router, "archive", None) is not None:
                    await router.archive.aclose()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.backlog_interval_seconds)

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

    @app.get("/archive/status")
    async def archive_status() -> dict[str, Any]:
        async with await db.connect(dsn) as conn:
            return {"enabled": settings.archive_enabled, **await archive_outbox.status(conn)}

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
    async def runs(limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
        """Latest runs first. `kind=daily` leaves out backlog drains, which start on their own."""
        async with await db.connect(dsn) as conn:
            return await db.list_runs(conn, limit, kind)

    @app.get("/backlog")
    async def backlog() -> dict[str, Any]:
        """How many papers still wait for a re-screen under the current selection versions."""
        async with await db.connect(dsn) as conn:
            selections = await active_selections(conn)
            pending = await db.backlog(conn, selections, settings.rescreen_window_days)
        return {"pending": len(pending), "enabled": settings.backlog_enabled}

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

    @app.get("/papers/demoted")
    async def demoted(since: datetime) -> list[dict[str, Any]]:
        """Papers that left membership since `since`: the projection archives their pages.

        Unpaged on purpose: the rows are small, and a cut page could never be asked for again."""
        async with await db.connect(dsn) as conn:
            return await db.list_demoted(conn, since)

    @app.get("/projection/changes")
    async def projection_changes(limit: int = 3000) -> dict[str, Any]:
        """Member papers changed since the projection's last acknowledgement, oldest first.

        The caller writes the rows, then puts `through` to `/projection/cursor`. `since` is the
        time to ask `/papers/demoted` for; `more` means another page is waiting."""
        async with await db.connect(dsn) as conn:
            return await db.projection_changes(conn, min(max(limit, 1), 5000))

    @app.put("/projection/cursor")
    async def put_projection_cursor(cursor: ProjectionCursor) -> dict[str, Any]:
        async with await db.connect(dsn) as conn:
            at, paper_id = await db.ack_projection(conn, cursor.changed_at, cursor.paper_id)
        return {"changed_at": at, "paper_id": paper_id}

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
