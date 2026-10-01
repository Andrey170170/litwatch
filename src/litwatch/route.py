"""Routing: papers whose best tier is 1 or 2 become Zotero items in the `Litwatch` collection.

Tier 1 also gets its arXiv PDF, attached fully server-side via the WebDAV layout. Routing is
idempotent: re-running converges collections, tags, and note, and never duplicates. A paper that
drops below tier 2 is released: taken out of the managed collections, its item left in place.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from litwatch.archive_client import (
    ArchiveClient,
    ArchiveError,
    ArchiveUnavailable,
    ArchiveVersionUnknown,
)
from litwatch.arxiv_access import async_get
from litwatch.models import ROUTED_TIERS, Paper, ScreenResult
from litwatch.webdav import WebDAVWriter
from litwatch.zotero import ZoteroClient, screen_note, selection_tags, webdav_payload

log = logging.getLogger(__name__)

PDF_TIERS = (1,)
ARXIV_PACING_SECONDS = 3.0


class PDFUnavailableError(RuntimeError):
    """Required tier-1 attachment is pending; keep routing eligible for retry."""


@dataclass(frozen=True)
class Routed:
    paper_id: str
    tier: int
    item_key: str
    attachment_key: str | None


class Router:
    def __init__(
        self,
        zotero: ZoteroClient,
        webdav: WebDAVWriter | None,
        http: httpx.AsyncClient | None = None,
        attach_pdf: bool = True,
        archive: ArchiveClient | None = None,
    ) -> None:
        self.archive = archive
        self.zotero = zotero
        self.webdav = webdav
        self.http = http or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        self.attach_pdf = attach_pdf and webdav is not None
        self._pdf_lock = asyncio.Lock()  # arXiv asks for one request at a time

    async def route(self, paper: Paper, result: ScreenResult) -> Routed:
        tier = result.best_tier
        if tier not in ROUTED_TIERS:
            raise ValueError(f"{paper.id} is tier {tier}, not routable")
        key = await self.zotero.ensure_item(paper, tier, selection_tags(result))
        await self.zotero.upsert_note(key, screen_note(paper, result))
        attachment = None
        if self.attach_pdf and tier in PDF_TIERS:
            attachment = await self._ensure_pdf(key, paper)
            if attachment is None:
                raise PDFUnavailableError(f"{paper.id}: required PDF is not available yet")
        return Routed(paper_id=paper.id, tier=tier, item_key=key, attachment_key=attachment)

    async def release(self, item_key: str) -> None:
        await self.zotero.release_item(item_key)

    async def _ensure_pdf(self, item_key: str, paper: Paper) -> str | None:
        """Attach once. An attachment already on the item (ours or the user's) is left alone."""
        assert self.webdav is not None
        existing = await self.zotero.pdf_attachment(item_key)
        if existing and await self.webdav.exists(existing):
            return existing
        if existing:
            log.warning("%s: attachment %s has no WebDAV file; leaving it", paper.id, existing)
            return existing
        pdf = await self._download(paper)
        if pdf is None:
            return None
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
                log.warning("%s: %s; using paced PDF fallback", paper.id, type(exc).__name__)
            except ArchiveError:
                log.warning("%s: archive PDF request failed; retrying on next route", paper.id)
                return None
        identity = f"{paper.id}v{paper.version}" if paper.version else paper.id
        async with self._pdf_lock:
            resp = await async_get(self.http, f"https://arxiv.org/pdf/{identity}")
            await asyncio.sleep(ARXIV_PACING_SECONDS)
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF-"):
            log.warning("%s: PDF download failed (%s)", paper.id, resp.status_code)
            return None
        return resp.content
