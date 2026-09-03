"""Writes attachment archives to the Zotero WebDAV share (KEY.zip + KEY.prop)."""

from __future__ import annotations

import httpx


class WebDAVError(Exception):
    pass


class WebDAVWriter:
    def __init__(
        self, url: str, username: str, password: str, http: httpx.AsyncClient | None = None
    ):
        self.base = url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=120.0, auth=(username, password))
        if http is not None:
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
            raise WebDAVError(f"PUT {name}: {resp.status_code} {resp.text[:200]}")
