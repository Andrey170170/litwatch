"""The service's HTTP side: the JSON API, and the web app it serves."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from litwatch import fulltext
from litwatch.api import digests, mcp, reading, settings, summary
from litwatch.api import integrations as integrations_api
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core import (
    SEARCH_TIMEOUT_SECONDS,
)
from litwatch.events import Integration, dispatch
from litwatch.fulltext import deep as deep_passes
from litwatch.jobs import scheduler
from litwatch.jobs.context import GatewayFactory, default_gateway
from litwatch.publications import Dblp, PublicationSource

# The built web app (`web/`, `pnpm build`), shipped inside the package.
WEB = Path(str(resources.files("litwatch.api").joinpath("web")))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the scheduler with the service when the configuration enables it, with the
    full-text worker when an archive is configured too (it calls models and has the archive
    fetch, so, like the schedule, it waits for cutover) and the Deep card worker when a card
    provider is loaded as well, the integrations' dispatcher when any is loaded, and the MCP
    server (its session manager and PDF executor) while `/mcp` is served."""
    tasks = []
    enabled = app.state.config.schedule.enabled
    archive = app.state.archive if enabled else None
    if archive is not None:
        tasks.append(
            asyncio.create_task(
                fulltext.run_forever(
                    app.state.dsn,
                    app.state.config,
                    archive,
                    app.state.gateway,
                    app.state.fulltext_wake,
                )
            )
        )
        if maker := integrations_api.card_maker(app.state.integrations):
            tasks.append(
                asyncio.create_task(
                    deep_passes.run_forever(app.state.dsn, archive, maker, app.state.deep_wake)
                )
            )
    if app.state.config.schedule.enabled:
        tasks.append(
            asyncio.create_task(
                scheduler.run_forever(
                    app.state.dsn, app.state.config, app.state.gateway, app.state.publications
                )
            )
        )
    if app.state.integrations:
        tasks.append(
            asyncio.create_task(dispatch.run_forever(app.state.dsn, app.state.integrations))
        )
    try:
        async with mcp.running(app.state) if app.state.mcp else contextlib.nullcontext():
            yield
    finally:
        for task in tasks:
            # Wait for the job in progress to record how it ended.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        app.state.search_pool.shutdown(wait=False, cancel_futures=True)
        if app.state.archive is not None:
            await app.state.archive.aclose()


def create_app(
    dsn: str,
    token: str,
    config: Config | None = None,
    gateway: GatewayFactory = default_gateway,
    publications: PublicationSource | None = None,
    integrations: list[Integration] | None = None,
    read_token: str | None = None,
    archive: ArchiveClient | None = None,
    mcp_token: str | None = None,
) -> FastAPI:
    """The app over one database. Every request but the health check and signing in needs
    the shared token, as a bearer header or the cookie set by `/api/session`; `read_token`,
    when given, may only read papers (`deps.READABLE`). `integrations` are the loaded ones:
    the dispatcher delivers to them and search asks those that are search providers.
    `archive` serves the PDFs the owner opens, and the full-text queue's texts; the app
    closes it when it stops. `mcp_token`, when given, serves the MCP server at `/mcp`
    (`api.mcp`), which takes that token alone, as `/api` never does."""
    if mcp_token is not None and mcp_token in (token, read_token):
        raise ValueError("the MCP token must differ from the API token and the read token")
    app = FastAPI(title="litwatch", docs_url=None, redoc_url=None, lifespan=_lifespan)
    app.state.dsn, app.state.token, app.state.read_token = dsn, token, read_token
    app.state.config, app.state.gateway = config or Config(), gateway
    app.state.publications = publications or Dblp()
    app.state.integrations = integrations or []
    app.state.archive = archive
    app.state.search_timeout = SEARCH_TIMEOUT_SECONDS
    app.state.search_pool = integrations_api.search_pool()
    app.state.search_stats = {}
    app.state.fulltext_wake = asyncio.Event()  # set to have the full-text worker look now
    app.state.deep_wake = asyncio.Event()  # and the Deep card worker
    # Each router but the sign-in asks for the token (`deps.protected`).
    app.include_router(reading.router)
    app.include_router(digests.router)
    app.include_router(settings.router)
    app.include_router(integrations_api.router)
    app.include_router(summary.router)
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
