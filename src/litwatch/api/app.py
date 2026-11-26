"""The service's HTTP side: the JSON API, and the web app it serves."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from litwatch import events
from litwatch.api import digests, jobs, mcp, reading, settings, summary
from litwatch.api import integrations as integrations_api
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core import (
    SEARCH_TIMEOUT_SECONDS,
)
from litwatch.events import Integration
from litwatch.events.worker import DeliveryWorker
from litwatch.fulltext.worker import DeepPassWorker, TextPassWorker
from litwatch.gates.context import bookkeeping
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import Worker, WorkGate
from litwatch.jobs import scheduler
from litwatch.jobs.context import model_gate
from litwatch.jobs.worker import JobWorker
from litwatch.llm import ModelGate
from litwatch.publications import Dblp, PublicationSource
from litwatch.store import fulltext as fulltext_store
from litwatch.store import jobs as jobs_store
from litwatch.store import outbox

log = logging.getLogger(__name__)

# The built web app (`web/`, `pnpm build`), shipped inside the package.
WEB = Path(str(resources.files("litwatch.api").joinpath("web")))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """The app's Database gate, open while it serves: opened before anything starts, closed
    after everything has stopped, when the app owns it. A gate given to `create_app` is its
    giver's to open and close."""
    async with app.state.db if app.state.owns_db else contextlib.nullcontext(), _running(app):
        yield


@asynccontextmanager
async def _running(app: FastAPI) -> AsyncIterator[None]:
    """One Work gate for the Workers the configuration enables: with the schedule on, the
    scheduler, its fires (the nightly, the publication lookup) and the jobs; full-text passes
    when an archive is configured too (they call models and have the archive fetch, so, like
    the schedule, they wait for cutover), Deep passes when a card provider is loaded as well;
    and deliveries when any integration that reacts to events is loaded. Each kind is swept for
    what waits before the gate starts. And the MCP server (its session manager and PDF executor)
    while `/mcp` is served. Once the gate has stopped, the jobs' open runs are closed, then
    every integration, also when a sweep failed.

    Every model call of the process goes through one Model gate (`app.state.models`): the one
    given to `create_app`, else one built here on the app's Database gate and closed once
    everything has stopped. Before any work starts, it takes over the batches left open that no
    live claim will read (`ModelGate.recover`), once for the process's life."""
    db, config = app.state.db, app.state.config
    owned = app.state.models is None
    models: ModelGate = app.state.models or model_gate(db, config)
    app.state.models = models
    workers: dict[str, Worker] = {}
    swept: list[Callable[[], Awaitable[None]]] = []
    enabled = config.schedule.enabled
    archive = app.state.archive if enabled else None
    job_worker = None
    if enabled:
        schedule = scheduler.ScheduleWorker(db, config)
        job_worker = JobWorker(db, config, models, archive)
        workers |= {
            scheduler.SCHEDULE: schedule,
            "nightly": scheduler.NightlyWorker(db, config, models),
            "publications": scheduler.PublicationsWorker(db, app.state.publications),
            jobs_store.WORK: job_worker,
        }
        swept += [schedule.sweep, job_worker.sweep]
    if archive is not None:
        text = workers[fulltext_store.TEXT_WORK] = TextPassWorker(db, config, archive, models)
        swept.append(text.sweep)
        if maker := integrations_api.card_maker(app.state.integrations):
            deep = workers[fulltext_store.DEEP_WORK] = DeepPassWorker(db, config, archive, maker)
            swept.append(deep.sweep)
    keys: dict[str, list[str]] = {}
    if reacting := events.reacting(app.state.integrations):
        delivery = workers[outbox.DELIVERY] = DeliveryWorker(db, reacting)
        keys[outbox.DELIVERY] = delivery.keys  # another process's integrations are not ours
        swept.append(delivery.sweep)
    gate = None
    try:
        await _recover(models, config)
        for sweep in swept:
            await sweep()
        if workers:
            gate = asyncio.create_task(WorkGate(db).run(workers, keys=keys))
        async with mcp.running(app.state) if app.state.mcp else contextlib.nullcontext():
            yield
    finally:
        if gate is not None:
            # Each item running is stopped and given back once it has recorded how it ended.
            gate.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await gate
        if job_worker is not None:
            await job_worker.close(RuntimeError("the service stopped"))
        if owned:
            await models.close()
            app.state.models = None
        app.state.search_pool.shutdown(wait=False, cancel_futures=True)
        for integration in app.state.integrations:
            if close := getattr(integration, "aclose", None):
                with contextlib.suppress(Exception):
                    await close()
        if app.state.archive is not None:
            await app.state.archive.aclose()


async def _recover(models: ModelGate, config: Config) -> None:
    """The batches left open, taken over as the service starts, under the Model gate's own
    attribution: waited for until tonight's deadline when it starts inside the night's window,
    so a nightly run again tonight shares them, else cancelled at once and read as they end."""
    now = datetime.now(UTC)
    night = config.schedule.night(now)
    with bookkeeping("model gate: recover"):
        if found := await models.recover(night[1] if night else now):
            log.info("model gate: %d batches left open are read as they end", found)


def create_app(
    dsn: str,
    token: str,
    config: Config | None = None,
    models: ModelGate | None = None,
    publications: PublicationSource | None = None,
    integrations: list[Integration] | None = None,
    read_token: str | None = None,
    archive: ArchiveClient | None = None,
    mcp_token: str | None = None,
    db: DatabaseGate | None = None,
) -> FastAPI:
    """The app over one database, through one Database gate (`app.state.db`): `db`, open
    while the app serves, or else one of its own on `dsn`, which its lifespan opens and
    closes (`serve` gives the process's one); `models`, the process's Model gate when the
    caller built it (a test), else the app builds its own as it starts. Every request but the
    health check and signing
    in needs the shared token, as a bearer header or the cookie set by `/api/session`; `read_token`,
    when given, may only read papers (`deps.READABLE`). `integrations` are the loaded ones:
    the Work gate delivers to those that react to events, search asks those that are search
    providers, and the app closes them all when it stops.
    `archive` serves the PDFs the owner opens, and the full-text queue's texts; the app
    closes it when it stops. `mcp_token`, when given, serves the MCP server at `/mcp`
    (`api.mcp`), which takes that token alone, as `/api` never does."""
    if mcp_token is not None and mcp_token in (token, read_token):
        raise ValueError("the MCP token must differ from the API token and the read token")
    app = FastAPI(
        title="litwatch", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan
    )
    app.state.token, app.state.read_token = token, read_token
    # The routes, MCP, the monitor and the loops all use the one gate.
    app.state.db, app.state.owns_db = db or DatabaseGate(dsn), db is None
    app.state.config, app.state.models = config or Config(), models
    app.state.publications = publications or Dblp()
    app.state.integrations = integrations or []
    app.state.archive = archive
    app.state.search_timeout = SEARCH_TIMEOUT_SECONDS
    app.state.search_pool = integrations_api.search_pool()
    app.state.search_stats = {}
    # Each router but the sign-in asks for the token (`deps.protected`).
    app.include_router(reading.router)
    app.include_router(digests.router)
    app.include_router(settings.router)
    app.include_router(integrations_api.router)
    app.include_router(summary.router)
    app.include_router(jobs.router)
    app.include_router(reading.session)
    # Before the web app's catch-all, which would shadow it.
    app.state.mcp = app.state.mcp_calls = None
    if mcp_token:
        app.state.mcp = mcp.mount(app, mcp_token, app.state.config.mcp.allowed_hosts)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    _serve_web(app)
    return app


def _serve_web(app: FastAPI) -> None:
    """The web app's files, and its index for every other path so client-side routes such as
    `/share?url=...` (the share target) load the app. Unknown `/api` paths stay 404."""
    if not (WEB / "index.html").exists():

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def unbuilt() -> str:
            return (
                "<p>The web app is not built. Run <code>pnpm build</code> in <code>web/</code>.</p>"
            )

        return
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        file = (WEB / path).resolve()
        if path and file.is_file() and file.is_relative_to(WEB.resolve()):
            return FileResponse(file)
        return FileResponse(WEB / "index.html")
