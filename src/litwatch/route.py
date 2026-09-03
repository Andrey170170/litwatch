"""Routing: tier 1 and 2 papers become Zotero items in the `Litwatch` collection.

Tier 1 also gets its arXiv PDF, attached fully server-side via the WebDAV layout.
Routing is idempotent: re-running converges collections, tag, and note, and never duplicates.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from litwatch.models import Paper, ScreenResult, Tier
from litwatch.webdav import WebDAVWriter
from litwatch.zotero import ZoteroClient, screen_note, webdav_payload

log = logging.getLogger(__name__)

ROUTED_TIERS = (Tier.MUST_READ, Tier.AWARE)
PDF_TIERS = (Tier.MUST_READ,)
ARXIV_PACING_SECONDS = 3.0


@dataclass(frozen=True)
class Routed:
    paper_id: str
    tier: Tier
    item_key: str
    attachment_key: str | None


class Router:
    def __init__(
        self,
        zotero: ZoteroClient,
        webdav: WebDAVWriter | None,
        http: httpx.AsyncClient | None = None,
        attach_pdf: bool = True,
    ) -> None:
        self.zotero = zotero
        self.webdav = webdav
        self.http = http or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        self.attach_pdf = attach_pdf and webdav is not None
        self._pdf_lock = asyncio.Lock()  # arXiv asks for one request at a time

    async def route(self, paper: Paper, result: ScreenResult) -> Routed:
        tier = result.tier
        if tier not in ROUTED_TIERS:
            raise ValueError(f"{paper.id} is tier {tier}, not routable")
        key = await self.zotero.ensure_item(paper, tier)
        await self.zotero.upsert_note(key, screen_note(paper, result))
        attachment = None
        if self.attach_pdf and tier in PDF_TIERS:
            attachment = await self._ensure_pdf(key, paper)
        return Routed(paper_id=paper.id, tier=tier, item_key=key, attachment_key=attachment)

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
        async with self._pdf_lock:
            resp = await self.http.get(f"https://arxiv.org/pdf/{paper.id}")
            await asyncio.sleep(ARXIV_PACING_SECONDS)
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF-"):
            log.warning("%s: PDF download failed (%s)", paper.id, resp.status_code)
            return None
        return resp.content
