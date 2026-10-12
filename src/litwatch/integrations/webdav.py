"""webdav: stores the PDF of every `must_read` library paper on the WebDAV share Zotero's
file sync reads, attached to the paper's Zotero item.

It hears `grade.changed` to `must_read` and `paper.saved`, and acts only when the paper is,
at that moment, `must_read` for some Watch topic and in the library: an overruled or
superseded `must_read`, or an unsaved paper, fetches nothing, and saving it again does what
was skipped. The item is the `zotero` integration's, so this one needs it loaded; until the
item exists the event is retried. A PDF already attached (litwatch's or the owner's) is left
alone, so no paper gets two. The PDF comes from the archive when that integration is
configured, else from arXiv, one request at a time.

Options: `url`, `username` (or $WEBDAV_URL, $WEBDAV_USERNAME) and the password in
$WEBDAV_PASSWORD; the Zotero account is the `zotero` section's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import psycopg

from litwatch import events
from litwatch.config import Config
from litwatch.core import Event, Paper
from litwatch.integrations import (
    IntegrationError,
    must_read_and_saved,
    required,
    required_secret,
)
from litwatch.integrations import zotero as zotero_mod
from litwatch.integrations.archive import (
    ArchiveClient,
    ArchiveError,
    ArchiveUnavailable,
    ArchiveVersionUnknown,
    reader,
)
from litwatch.integrations.zotero import ZoteroClient, webdav_payload
from litwatch.store import integrations as state
from litwatch.store import papers as papers_store

log = logging.getLogger(__name__)

ARXIV_PACING_SECONDS = 3.0


class NotYetError(RuntimeError):
    """The work can be done later: the item is not made yet, or the PDF not available."""


class WebDAVError(Exception):
    pass


class WebDAVWriter:
    """Writes attachment archives to the Zotero WebDAV share (KEY.zip + KEY.prop)."""

    def __init__(
        self, url: str, username: str, password: str, http: httpx.AsyncClient | None = None
    ) -> None:
        self.base = url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=120.0)
        self.http.auth = httpx.BasicAuth(username, password)

    async def put_attachment(self, key: str, archive: bytes, prop: bytes) -> None:
        """Zip first, then prop: a client that sees the prop must be able to fetch the zip."""
        await self._put(f"{key}.zip", archive, "application/zip")
        await self._put(f"{key}.prop", prop, "text/xml")

    async def exists(self, key: str) -> bool:
        resp = await self.http.head(f"{self.base}/{key}.prop")
        return resp.status_code == 200

    async def _put(self, name: str, body: bytes, content_type: str) -> None:
        resp = await self.http.put(
            f"{self.base}/{name}", content=body, headers={"Content-Type": content_type}
        )
        if resp.status_code not in (200, 201, 204):
            raise WebDAVError(f"PUT {name}: {resp.status_code}")


class WebDav:
    name = "webdav"
    description = (
        "Stores the PDF of every must-read library paper on the WebDAV share Zotero's file "
        "sync reads, attached to its Zotero item."
    )
    requires = ("zotero",)

    def __init__(
        self,
        zotero: ZoteroClient,
        webdav: WebDAVWriter,
        *,
        name: str = "webdav",
        archive: ArchiveClient | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.zotero = zotero
        self.webdav = webdav
        self.archive = archive
        self.http = http or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        self._arxiv = asyncio.Lock()  # arXiv asks for one request at a time

    def wants(self, event: Event) -> bool:
        if event.type == events.PAPER_SAVED:
            return True
        return event.type == events.GRADE_CHANGED and event.payload.get("after") == "must_read"

    async def setup(self, conn: psycopg.AsyncConnection) -> None:
        if taken := await state.seed_webdav(conn):
            log.info("webdav: took over %d PDFs attached by 0.x", taken)

    async def aclose(self) -> None:
        for http in (self.http, self.zotero.http, self.webdav.http):
            await http.aclose()
        if self.archive is not None:
            await self.archive.aclose()

    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None:
        paper_id = event.paper_id
        if await state.webdav_file(conn, paper_id):
            return
        if not await must_read_and_saved(conn, paper_id):
            return
        item = await state.zotero_item(conn, paper_id)
        if item is None:
            raise NotYetError("the zotero integration has not made the item yet")
        paper = await papers_store.get(conn, paper_id)
        attachment = await self._ensure_pdf(item, paper)
        await state.set_webdav_file(conn, paper_id, attachment)

    async def _ensure_pdf(self, item_key: str, paper: Paper) -> str:
        """Attach once. An attachment already on the item (ours or the owner's) is left alone."""
        existing = await self.zotero.pdf_attachment(item_key)
        if existing:
            if not await self.webdav.exists(existing):
                log.warning("%s: attachment %s has no WebDAV file; leaving it", paper.id, existing)
            return existing
        pdf = await self._download(paper)
        if pdf is None:
            raise NotYetError("the PDF is not available yet")
        key = await self.zotero.create_attachment(item_key, paper, pdf)
        archive, prop = webdav_payload(paper, pdf)
        await self.webdav.put_attachment(key, archive, prop)
        log.info("%s: attached PDF %s (%d bytes)", paper.id, key, len(pdf))
        return key

    async def _download(self, paper: Paper) -> bytes | None:
        if self.archive is not None:
            try:
                return await self.archive.pdf(paper)
            except (ArchiveUnavailable, ArchiveVersionUnknown) as exc:
                log.warning("%s: %s; fetching from arXiv", paper.id, type(exc).__name__)
            except ArchiveError:
                log.warning("%s: archive PDF request failed; retrying later", paper.id)
                return None
        identity = f"{paper.id}v{paper.version}" if paper.version else paper.id
        async with self._arxiv:
            resp = await self.http.get(f"https://arxiv.org/pdf/{identity}")
            await asyncio.sleep(ARXIV_PACING_SECONDS)
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF-"):
            log.warning("%s: PDF download failed (%s)", paper.id, resp.status_code)
            return None
        return resp.content


def load(name: str, options: dict[str, Any], config: Config) -> WebDav:
    zotero_options = config.integrations.get("zotero")
    if zotero_options is None:
        raise IntegrationError("webdav attaches to Zotero items: configure [integrations.zotero]")
    return WebDav(
        zotero_mod.client(zotero_options),
        WebDAVWriter(
            required(options, "url", env="WEBDAV_URL"),
            required(options, "username", env="WEBDAV_USERNAME"),
            required_secret("WEBDAV_PASSWORD"),
        ),
        name=name,
        archive=reader(config),
    )
