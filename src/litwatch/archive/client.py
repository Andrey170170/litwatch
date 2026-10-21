"""arxiv-archive's authenticated API: catalog records, acquisition jobs, bounded PDF reads, a
paper's LaTeX text and the papers of a date window."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from litwatch.core import Paper

MAX_PDF_BYTES = 128 * 1024 * 1024
BACKGROUND = 100  # the archive's priority for papers nobody is waiting on
# A PDF the storage policy keeps (archive.md): above background, so an archive that holds
# background PDFs (`paper-archive pdfs wanted`) fetches it.
WANTED = 50
URGENT = 0  # for a paper a full-text pass is waiting on


class ArchiveError(RuntimeError):
    """An archive request was rejected or returned an invalid response."""


class ArchiveUnavailable(ArchiveError):  # noqa: N818 - public outage signal
    """The service cannot be reached; callers may fall back to paced direct access."""


class ArchiveVersionUnknown(ArchiveError):  # noqa: N818 - public resolution signal
    """No explicit revision can safely be associated with this metadata."""


@dataclass(frozen=True)
class PaperText:
    """A paper's full text as read: the arXiv version it is of (None when the archive cannot
    say, as for its historical LaTeX), where it came from and, for a PDF's, where each of its
    pages lies in `body` (`text.PdfText.pages`)."""

    body: str
    version: int | None
    source: Literal["latex", "pdf"]
    pages: tuple[tuple[int, int], ...] | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode()).hexdigest()


def _same_timestamp(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return datetime.fromisoformat(left.replace("Z", "+00:00")) == datetime.fromisoformat(
            right.replace("Z", "+00:00")
        )
    except ValueError:
        return left == right


def metadata(paper: Paper) -> dict[str, Any]:
    """The paper as the archive's catalog takes it; litwatch's own revision count stays home."""
    return paper.model_dump(mode="json", exclude_none=True, exclude={"revision"})


def _midnight(day: str | None) -> str | None:
    return datetime.combine(date.fromisoformat(day), time.min, UTC).isoformat() if day else None


def listed(item: dict[str, Any]) -> Paper:
    """A `/v1/papers` item as a paper. Its dates are days, taken as midnight UTC as the arXiv
    intake takes OAI's; a missing title or abstract is empty."""
    try:
        return Paper(
            id=item["id"],
            title=item.get("title") or "",
            abstract=item.get("abstract") or "",
            authors=item.get("authors") or [],
            categories=item.get("categories") or [],
            primary_category=item.get("primary_category"),
            published=_midnight(item.get("published")),
            updated=_midnight(item.get("updated")),
            url=f"https://arxiv.org/abs/{item['id']}",
            version=item.get("version"),
            version_history=item.get("version_history") or [],
            doi=item.get("doi"),
            journal_ref=item.get("journal_ref"),
            license=item.get("license"),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ArchiveError("invalid archive listing item") from exc


class ArchiveClient:
    """The archive's authenticated API: catalog records, acquisition jobs, bounded reads,
    window listings. Never puts response bodies, headers or credentials into an error."""

    def __init__(
        self,
        base_url: str,
        token: str,
        http: httpx.AsyncClient | None = None,
        *,
        wait_seconds: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self.http = http or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        self.wait_seconds = max(0.0, wait_seconds)

    async def aclose(self) -> None:
        await self.http.aclose()

    @staticmethod
    def _path(paper_id: str) -> str:
        return "/v1/papers/" + quote(paper_id, safe="")

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code >= 500 or response.status_code in (401, 403, 429):
            raise ArchiveUnavailable(f"archive HTTP {response.status_code}")
        if not response.is_success:
            raise ArchiveError(f"archive HTTP {response.status_code}")

    async def _json(
        self, method: str, path: str, *, missing: bool = False, **kwargs: Any
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

    async def record(self, paper_id: str) -> dict | None:
        return await self._json("GET", self._path(paper_id), missing=True)

    async def paper(self, paper_id: str) -> Paper | None:
        """The paper as the archive's catalog has it, None when the catalog lacks it."""
        found = await self.record(paper_id)
        if found is None:
            return None
        if not isinstance(found.get("item"), dict):
            raise ArchiveError("archive record without its listing item")
        return listed(found["item"])

    async def indexed(self) -> int | None:
        """How many papers the archive's search index holds; None before it has any."""
        found = await self._json("GET", "/v1/search/status")
        assert found is not None
        count = found.get("indexed_papers")
        return count if type(count) is int and count > 0 else None

    async def upsert_paper(self, paper: Paper) -> dict:
        result = await self._json(
            "PUT",
            self._path(paper.id),
            json={"metadata": metadata(paper), "latest_version": paper.version},
        )
        assert result is not None
        return result

    async def resolve(self, paper: Paper) -> Paper:
        """Reuse the catalog's revision only when it describes the same upstream update."""
        if paper.version is not None:
            return paper
        found = await self.record(paper.id)
        if (
            found
            and found.get("latest_version")
            and _same_timestamp(paper.updated, found.get("metadata", {}).get("updated"))
        ):
            return paper.model_copy(update={"version": found["latest_version"]})
        raise ArchiveVersionUnknown(f"{paper.id}: explicit revision unavailable")

    async def request(self, paper: Paper, kind: str = "pdf", priority: int = BACKGROUND) -> dict:
        """Ask for an artifact of an explicit revision; asking again promotes the same job."""
        assert paper.version is not None
        result = await self._json(
            "POST",
            "/v1/jobs",
            json={
                "paper_id": paper.id,
                "version": paper.version,
                "kind": kind,
                "priority": priority,
                "provenance": {"client": "litwatch"},
            },
        )
        assert result is not None
        return result

    async def retry(self, job_id: int | str) -> dict:
        """Try a failed acquisition again, its attempts reset (the archive's operator retry)."""
        result = await self._json("POST", f"/v1/jobs/{int(job_id)}/retry", json={})
        assert result is not None
        return result

    async def job(self, job_id: int | str) -> dict:
        result = await self._json("GET", f"/v1/jobs/{int(job_id)}")
        assert result is not None
        return result

    async def available_pdf(self, paper_id: str, version: int) -> bytes | None:
        """Read an explicit revision, bounding the transfer."""
        try:
            async with self.http.stream(
                "GET",
                self.base_url + self._path(paper_id) + "/pdf",
                headers=self._headers,
                params={"version": version},
            ) as response:
                if response.status_code == 404:
                    return None
                self._check(response)
                result = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(result) + len(chunk) > MAX_PDF_BYTES:
                        raise ArchiveError("archive PDF exceeds the read limit")
                    result.extend(chunk)
        except httpx.HTTPError as exc:
            raise ArchiveUnavailable("archive PDF transport unavailable") from exc
        if not result.startswith(b"%PDF-"):
            raise ArchiveError("archive returned an invalid PDF")
        return bytes(result)

    async def latex(self, paper_id: str, version: int | None = None) -> PaperText | None:
        """The text of a paper's LaTeX source: of `version` (0: its historical LaTeX, of no
        known version), or without one the newest the archive has (its historical LaTeX last).
        None when it has none."""
        params = {"version": version} if version is not None else {}
        body = await self._json("GET", self._path(paper_id) + "/text", missing=True, params=params)
        if body is None:
            return None
        text, found = body.get("text"), body.get("version")
        if not isinstance(text, str) or not (found is None or type(found) is int):
            raise ArchiveError("invalid archive text")
        if version is not None and found != (version or None):
            raise ArchiveError("archive text of another version")
        return PaperText(text, found, "latex")

    async def pdf(self, paper: Paper) -> bytes | None:
        """The PDF, asked for urgently and waited on for at most `wait_seconds`. None when the
        archive could not get it in time; a pending job is never duplicated by a caller."""
        paper = await self.resolve(paper)
        job = await self.request(paper, priority=0)
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

    async def papers(
        self, since: date, until: date, categories: Iterable[str] = (), *, limit: int = 500
    ) -> AsyncIterator[list[Paper]]:
        """The papers first submitted from `since` to `until`, both inclusive, in any of
        `categories` (every category when there are none), a page at a time in the archive's
        order (date, then id). Pages follow the archive's cursor to the end of the window."""
        params: dict[str, Any] = {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "limit": limit,
        }
        if categories:
            params["categories"] = ",".join(categories)
        while True:
            body = await self._json("GET", "/v1/papers", params=params)
            assert body is not None
            items, cursor = body.get("items"), body.get("next")
            if not isinstance(items, list) or not isinstance(cursor, str | None):
                raise ArchiveError("invalid archive listing")
            if items:
                yield [listed(item) for item in items]
            if cursor is None:
                return
            params["cursor"] = cursor
