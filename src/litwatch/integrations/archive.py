"""archive: registers every new or revised paper with arxiv-archive, which keeps its PDF and
source, asks for the PDFs the storage policy keeps, and answers searches over the archive's
titles and abstracts.

On `paper.added` and `paper.revised` it publishes the paper's metadata and asks for its PDF
and source at background priority; the archive owns acquisition and deduplicates by paper,
version and kind, so asking twice is harmless. An artifact needs an explicit arXiv version:
when neither the paper nor the archive's catalog knows one, only the metadata is published.
Registration is independent of relevance: every paper intake stores is registered, except
what intake read from the archive itself (the event's `source` is "archive"): it is there.

The storage policy (archive.md) is the archive's to apply: under `paper-archive pdfs wanted`
it holds PDFs asked for at background priority. On `grade.changed` to relevant or must read,
on `paper.saved`, and on a revision (read from arXiv or from the archive), this asks for the
paper's PDF at `WANTED` priority, above background, when the paper qualifies at the time the
event is handled (`store.word.keeps_pdf`). A PDF the owner opens is asked for urgently by the
API, and one a full-text pass falls back to by the queue.

It is also a `SearchProvider`: litwatch's search box asks it beside the collection, which
adds the papers litwatch never ingested. Other integrations (webdav, deep) read PDFs through
the client (`litwatch.archive`) when this one is configured, with arXiv as the fallback.

Options: `url` (or $LITWATCH_ARCHIVE_URL), the bearer token in $LITWATCH_ARCHIVE_TOKEN,
`wait_seconds` (60: how long a reader waits for a PDF the archive is still fetching),
`search_timeout_seconds` (4, kept below search's own wait for a provider, so the archive's
request gives up first and frees its thread) and `register` (true; false registers nothing,
neither metadata nor PDF and source, leaving search and PDF reads: for running beside another
litwatch that registers the same papers).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg

from litwatch import events
from litwatch.archive import (
    BACKGROUND,
    WANTED,
    ArchiveClient,
    ArchiveUnavailable,
    ArchiveVersionUnknown,
)
from litwatch.config import Config
from litwatch.core import SEARCH_TIMEOUT_SECONDS, Event, SearchFilters, SearchHit
from litwatch.integrations import (
    IntegrationError,
    enabled_using,
    option,
    required,
    required_secret,
)
from litwatch.store import papers as papers_store
from litwatch.store import word as word_store

log = logging.getLogger(__name__)

ARTIFACTS = ("pdf", "source")
KEPT = ("relevant", "must_read")  # the grades whose papers' PDFs the storage policy keeps


def _registers(event: Event) -> bool:
    """A new or revised paper the archive does not have yet: intake read it from arXiv."""
    return (
        event.type in (events.PAPER_ADDED, events.PAPER_REVISED)
        and event.payload.get("source") != papers_store.ARCHIVE
    )


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


def source(config: Config) -> ArchiveClient:
    """A client for reading papers from the archive: the enabled archive section's, else one
    from $LITWATCH_ARCHIVE_URL and $LITWATCH_ARCHIVE_TOKEN alone."""
    return client(enabled_using(config, "archive") or {})


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
        return self.register and (
            _registers(event)
            or event.type in (events.PAPER_REVISED, events.PAPER_SAVED)
            or (event.type == events.GRADE_CHANGED and event.payload.get("after") in KEPT)
        )

    async def aclose(self) -> None:
        await self.archive.aclose()
        self.search_http.close()

    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None:
        paper = await papers_store.get(conn, event.paper_id)
        if paper is None:
            return
        # Read when handled: a grade already taken back asks for nothing.
        keep = await word_store.keeps_pdf(conn, paper.id)
        if _registers(event):
            await self.archive.upsert_paper(paper)
        elif not keep:
            return
        try:
            paper = await self.archive.resolve(paper)
        except ArchiveVersionUnknown:
            log.info("archive: %s has no known arXiv version; nothing to fetch", paper.id)
            return
        if _registers(event):
            for kind in ARTIFACTS:
                kept = kind == "pdf" and keep
                await self.archive.request(paper, kind, WANTED if kept else BACKGROUND)
        else:  # saved, graded up, or a revision the archive already has: only its PDF
            await self.archive.request(paper, "pdf", WANTED)

    def search(self, query: str, filters: SearchFilters) -> list[SearchHit]:
        params: dict[str, Any] = {"q": query, "limit": min(filters.limit, 50)}
        if filters.since:
            params["date_from"] = filters.since.isoformat()
        if filters.until:
            params["date_to"] = filters.until.isoformat()
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
