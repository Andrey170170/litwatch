"""archive: registers every new or revised paper with arxiv-archive, which keeps its PDF and
source, and answers searches over the archive's titles and abstracts.

On `paper.added` and `paper.revised` it publishes the paper's metadata and asks for its PDF
and source at background priority; the archive owns acquisition and deduplicates by paper,
version and kind, so asking twice is harmless. An artifact needs an explicit arXiv version:
when neither the paper nor the archive's catalog knows one, only the metadata is published.
Registration is independent of relevance: every paper intake stores is registered.

It is also a `SearchProvider`: litwatch's search box asks it beside the collection, which
adds the papers litwatch never ingested. Other integrations (webdav, deep) read PDFs through
`ArchiveClient` when this one is configured, with arXiv as the fallback.

Options: `url` (or $LITWATCH_ARCHIVE_URL), the bearer token in $LITWATCH_ARCHIVE_TOKEN,
`wait_seconds` (60: how long a reader waits for a PDF the archive is still fetching),
`search_timeout_seconds` (4, kept below search's own wait for a provider, so the archive's
request gives up first and frees its thread) and `register` (true; false registers nothing,
neither metadata nor PDF and source, leaving search and PDF reads: for running beside another
litwatch that registers the same papers).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

import httpx
import psycopg

from litwatch import events
from litwatch.config import Config
from litwatch.core import SEARCH_TIMEOUT_SECONDS, Event, Paper, SearchFilters, SearchHit
from litwatch.integrations import (
    IntegrationError,
    enabled_using,
    option,
    required,
    required_secret,
)
from litwatch.store import papers as papers_store

log = logging.getLogger(__name__)

MAX_PDF_BYTES = 128 * 1024 * 1024
ARTIFACTS = ("pdf", "source")
BACKGROUND = 100  # the archive's priority for papers nobody is waiting on; 0 is urgent


class ArchiveError(RuntimeError):
    """An archive request was rejected or returned an invalid response."""


class ArchiveUnavailable(ArchiveError):  # noqa: N818 - public outage signal
    """The service cannot be reached; callers may fall back to paced direct access."""


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


def metadata(paper: Paper) -> dict[str, Any]:
    """The paper as the archive's catalog takes it; litwatch's own revision count stays home."""
    return paper.model_dump(mode="json", exclude_none=True, exclude={"revision"})


class ArchiveClient:
    """The archive's authenticated API: catalog records, acquisition jobs, bounded reads.
    Never puts response bodies, headers or credentials into an error."""

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


def client(options: dict[str, Any]) -> ArchiveClient:
    """A client from an `[integrations.archive]` section and the environment."""
    return ArchiveClient(
        required(options, "url", env="LITWATCH_ARCHIVE_URL"),
        required_secret("LITWATCH_ARCHIVE_TOKEN"),
        wait_seconds=float(options.get("wait_seconds", 60.0)),
    )


def reader(config: Config) -> ArchiveClient | None:
    """A client for reading PDFs when an archive integration is configured and enabled."""
    section = enabled_using(config, "archive")
    return client(section) if section else None


def _authors(value: Any) -> list[str]:
    """The search index keeps authors as one string, "A, B and C"; a list is taken as is."""
    if isinstance(value, list):
        return [str(a) for a in value]
    return [a.strip() for a in re.split(r",\s*|\s+and\s+", str(value or "")) if a.strip()]


def _published(value: Any) -> datetime | None:
    """The index keeps a date; a hit carries it as midnight UTC."""
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
    return at.replace(tzinfo=UTC) if at and at.tzinfo is None else at


def hits(body: dict[str, Any]) -> list[SearchHit]:
    """`/v1/search` items as hits. The archive ranks by relevance but gives no score, so a hit
    carries its rank as one: the first is best."""
    items = body.get("items") or []
    return [
        SearchHit(
            id=item["id"],
            title=item.get("title") or "",
            authors=_authors(item.get("authors")),
            published=_published(item.get("published")),
            score=float(len(items) - rank),
        )
        for rank, item in enumerate(items)
    ]


class Archive:
    name = "archive"
    description = (
        "Registers every new or revised paper with arxiv-archive, which keeps its PDF and "
        "source, and adds the archive's papers to search."
    )

    def __init__(
        self,
        archive: ArchiveClient,
        *,
        name: str = "archive",
        search_http: httpx.Client | None = None,
        search_timeout: float = 4.0,
        register: bool = True,
    ) -> None:
        self.name = name
        self.archive = archive
        self.search_http = search_http or httpx.Client(timeout=search_timeout)
        self.register = register

    def wants(self, event: Event) -> bool:
        return self.register and event.type in (events.PAPER_ADDED, events.PAPER_REVISED)

    async def aclose(self) -> None:
        await self.archive.aclose()
        self.search_http.close()

    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None:
        paper = await papers_store.get(conn, event.paper_id)
        if paper is None:
            return
        await self.archive.upsert_paper(paper)
        try:
            paper = await self.archive.resolve(paper)
        except ArchiveVersionUnknown:
            log.info("archive: %s has no known arXiv version; metadata only", paper.id)
            return
        for kind in ARTIFACTS:
            await self.archive.request(paper, kind)

    def search(self, query: str, filters: SearchFilters) -> list[SearchHit]:
        params: dict[str, Any] = {"q": query, "limit": min(filters.limit, 50)}
        if filters.year:
            params |= {
                "date_from": date(filters.year, 1, 1).isoformat(),
                "date_to": date(filters.year, 12, 31).isoformat(),
            }
        try:
            response = self.search_http.get(
                f"{self.archive.base_url}/v1/search", params=params, headers=self.archive._headers
            )
        except httpx.HTTPError as exc:
            raise ArchiveUnavailable("archive search unavailable") from exc
        ArchiveClient._check(response)
        return hits(response.json())


def load(name: str, options: dict[str, Any], config: Config) -> Archive:
    timeout = float(option(options, "search_timeout_seconds") or 4.0)
    if not 0 < timeout < SEARCH_TIMEOUT_SECONDS:
        raise IntegrationError(
            f"search_timeout_seconds must be above zero and below {SEARCH_TIMEOUT_SECONDS:g}, "
            "how long search waits for a provider"
        )
    register = options.get("register", True)
    if not isinstance(register, bool):
        raise IntegrationError("register must be true or false")
    return Archive(client(options), name=name, search_timeout=timeout, register=register)
