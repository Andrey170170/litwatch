"""Internal archive API client: versioned artifacts, priority promotion, bounded reads."""

from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import quote

import httpx

from litwatch.models import Paper

MAX_PDF_BYTES = 128 * 1024 * 1024


class ArchiveError(RuntimeError):
    """An archive request was rejected or returned an invalid response."""


class ArchiveUnavailable(ArchiveError):  # noqa: N818 - public outage signal
    """The service cannot be reached; curated callers may use paced direct access."""


class ArchiveVersionUnknown(ArchiveError):  # noqa: N818 - public resolution signal
    """No explicit revision can safely be associated with this metadata."""


def _same_timestamp(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return datetime.fromisoformat(left.replace("Z", "+00:00")) == datetime.fromisoformat(
            right.replace("Z", "+00:00")
        )
    except ValueError:
        return left == right


class ArchiveClient:
    def __init__(
        self, base_url: str, token: str, http: httpx.AsyncClient | None = None,
        *, wait_seconds: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self.http = http or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        self._owns_http = http is None
        self.wait_seconds = max(0.0, wait_seconds)

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    @staticmethod
    def _path(paper_id: str) -> str:
        return "/v1/papers/" + quote(paper_id, safe="")

    @staticmethod
    def _check(response: httpx.Response) -> None:
        # Never propagate response bodies, request headers, or credentials into logs.
        if response.status_code >= 500 or response.status_code in (401, 403, 429):
            raise ArchiveUnavailable(f"archive HTTP {response.status_code}")
        if not response.is_success:
            raise ArchiveError(f"archive HTTP {response.status_code}")

    async def _json(
        self, method: str, path: str, *, missing: bool = False, **kwargs
    ) -> dict | None:
        try:
            response = await self.http.request(
                method, self.base_url + path, headers=self._headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ArchiveUnavailable("archive transport unavailable") from exc
        if missing and response.status_code == 404:
            return None
        self._check(response)
        try:
            result = response.json()
        except ValueError as exc:
            raise ArchiveError("invalid archive JSON") from exc
        if not isinstance(result, dict):
            raise ArchiveError("invalid archive object")
        return result

    async def metadata(self, paper_id: str) -> dict | None:
        return await self._json("GET", self._path(paper_id), missing=True)

    async def upsert_paper(self, paper: Paper) -> dict:
        result = await self._json("PUT", self._path(paper.id), json={
            "metadata": paper.model_dump(mode="json", exclude_none=True, exclude_unset=True),
            "latest_version": paper.version,
        })
        assert result is not None
        return result

    async def resolve(self, paper: Paper) -> Paper:
        """Reuse catalog revision only when it describes the same upstream update."""
        if paper.version is not None:
            return paper
        record = await self.metadata(paper.id)
        if record and record.get("latest_version") and _same_timestamp(
            paper.updated, record.get("metadata", {}).get("updated")
        ):
            return paper.model_copy(update={"version": record["latest_version"]})
        raise ArchiveVersionUnknown(f"{paper.id}: explicit revision unavailable")

    async def request(
        self, paper: Paper, kind: str = "pdf", priority: int = 100,
        *, publish_metadata: bool = True,
    ) -> dict:
        """Enqueue an artifact; outbox callers publish canonical metadata separately."""
        paper = await self.resolve(paper)
        if publish_metadata:
            await self.upsert_paper(paper)
        result = await self._json("POST", "/v1/jobs", json={
            "paper_id": paper.id, "version": paper.version, "kind": kind,
            "priority": priority, "provenance": {"client": "litwatch"},
        })
        assert result is not None
        return result

    async def job(self, job_id: int | str) -> dict:
        result = await self._json("GET", f"/v1/jobs/{int(job_id)}")
        assert result is not None
        return result

    async def available_pdf(self, paper_id: str, version: int) -> bytes | None:
        """Read an explicit revision, bounding decompressed transfer size."""
        try:
            async with self.http.stream(
                "GET", self.base_url + self._path(paper_id) + "/pdf",
                headers=self._headers, params={"version": version},
            ) as response:
                if response.status_code == 404:
                    return None
                self._check(response)
                result = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(result) + len(chunk) > MAX_PDF_BYTES:
                        raise ArchiveError("archive PDF exceeds curated read limit")
                    result.extend(chunk)
        except httpx.HTTPError as exc:
            raise ArchiveUnavailable("archive PDF transport unavailable") from exc
        if not result.startswith(b"%PDF-"):
            raise ArchiveError("archive returned an invalid PDF")
        return bytes(result)

    async def prepare_pdf(self, paper: Paper | str) -> tuple[Paper, dict]:
        """Promote immediately; the archive deduplicates by paper, version, and kind."""
        if isinstance(paper, str):
            record = await self.metadata(paper)
            if record is None or not record.get("latest_version"):
                raise ArchiveVersionUnknown(f"{paper}: explicit revision unavailable")
            # Imported historical author/date encodings need not match Litwatch's schema.
            # Only identity and explicit revision are needed for an ID-only read.
            data = record.get("metadata", {})
            paper = Paper(id=paper, version=record["latest_version"],
                          title=data.get("title") or "", abstract=data.get("abstract") or "")
        paper = await self.resolve(paper)
        return paper, await self.request(paper, priority=0)

    async def wait_pdf(self, paper: Paper, job: dict) -> bytes | None:
        """A pending job never triggers a second direct download on timeout."""
        assert paper.version is not None
        deadline = asyncio.get_running_loop().time() + self.wait_seconds
        while True:
            if job.get("status") == "complete":
                return await self.available_pdf(paper.id, paper.version)
            if job.get("status") in ("failed", "unavailable"):
                return None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(2.0, remaining))
            job = await self.job(job["id"])

    async def pdf(self, paper: Paper) -> bytes | None:
        paper, job = await self.prepare_pdf(paper)
        return await self.wait_pdf(paper, job)
