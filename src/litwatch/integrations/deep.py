"""deep: the deep service's Deep cards (docs/specs/v1/integrations.md). Which papers get a card,
and when, is the full-text queue's (`litwatch.fulltext.deep`): a card is a pass like a Brief,
so it waits for the paper's text and is re-checked under the paper's lock before it runs. This
module is how one is asked for: what a submission carries (the paper's text from the archive,
the Focus topics the card is for and the card extensions they ask for), and the service's job
protocol, followed to the job's end with its usage.

The service is a stateless job runner that does not deduplicate, so the queue records each
submission as an attempt before it is sent and the job once accepted, and never submits twice
for one job. It reacts to no event: `card.requested` is the owner's request already queued, and
`card.ready` is emitted when the card is stored.

Options: `url` (or $LITWATCH_DEEP_URL), `extensions` ({topic slug: extension}), `max_per_day`
(10: automatic submissions in any 24 hours, to protect the subscription from a loose rubric;
the owner's requests do not count), `poll_seconds` (15) and `timeout_minutes` (45).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg

from litwatch.archive import PaperText
from litwatch.config import Config
from litwatch.core import Depth, Event, Grade, Paper, Status, Topic
from litwatch.integrations import IntegrationError, enabled_using, required, required_secret
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import word as word_store

log = logging.getLogger(__name__)

# Card extensions per Focus topic; the names are the deep service's EXTENSIONS.
EXTENSIONS = {"cot-faithfulness": "faithfulness", "cot-monitoring": "monitoring"}
# A pass's lease outlasts following its job to the end by this much.
LEASE_MARGIN = timedelta(minutes=15)


@dataclass
class Outcome:
    """How a job ended. `usage` is the job's usage as the service reported it (turns, tokens,
    list cost); None when it never reported (the job was lost, or litwatch gave up waiting)."""

    result: dict[str, Any] | None
    trace: list[dict[str, Any]]
    usage: dict[str, Any] | None
    model: str
    error: str | None = None


class DeepClient:
    """HTTP client for the deep service: submit a job, follow its progress, collect it."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient | None = None,
        poll_seconds: float = 15.0,
        timeout_seconds: float = 45 * 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=60.0)
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    async def submit(self, submission: dict[str, Any]) -> str:
        """Submit one paper; returns the job's id. The service does not deduplicate, so a
        caller records the attempt before and the job after, and never submits twice."""
        resp = await self.http.post(f"{self.base_url}/process", json=submission)
        resp.raise_for_status()
        return str(resp.json()["id"])

    async def follow(self, job_id: str, since: datetime, label: str = "") -> Outcome:
        """Poll a job until it ends, or until `timeout_seconds` after `since` (when it was
        submitted). A failed poll raises, and the job can be followed again later, until then."""
        deadline = since + timedelta(seconds=self.timeout_seconds)
        gave_up = Outcome(None, [], None, "", "litwatch gave up waiting for the deep job")
        seen = ""
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                resp = await self.http.get(f"{self.base_url}/jobs/{job_id}")
                if resp.status_code == 404:
                    return Outcome(None, [], None, "", "deep job vanished (service restarted?)")
                resp.raise_for_status()
            except httpx.HTTPError:
                if datetime.now(UTC) > deadline:
                    return gave_up  # what it spent was never reported
                raise
            job = resp.json()
            stats = job.get("stats") or {}
            mark = f"{job['status']} {job.get('last_event', '')} cells={stats.get('cells', 0)}"
            if mark != seen:
                log.info(
                    "%s: %s turns=%s tokens=%s/%s list_cost=$%.2f %ss",
                    label,
                    mark,
                    stats.get("turns", 0),
                    stats.get("input", 0),
                    stats.get("output", 0),
                    stats.get("list_cost", 0.0),
                    stats.get("seconds", 0),
                )
                seen = mark
            if job["status"] in ("done", "error"):
                return Outcome(
                    job.get("result"),
                    job.get("trace") or [],
                    job.get("stats"),
                    job.get("model") or "",
                    job.get("error"),
                )
            if datetime.now(UTC) > deadline:
                return gave_up  # the job may go on spending; what it spent was never reported


class Deep:
    name = "deep"
    description = (
        "Writes a Deep card for every saved Focus must read, and for any paper the owner asks "
        "about, through the full-text queue."
    )

    def __init__(
        self,
        client: DeepClient,
        *,
        name: str = "deep",
        extensions: dict[str, str] | None = None,
        max_per_day: int = 10,
    ) -> None:
        self.name = name
        self.client = client
        self.extensions = EXTENSIONS if extensions is None else extensions
        self.max_per_day = max_per_day

    @property
    def lease(self) -> timedelta:
        """How long a Deep pass is held: past the longest a job is followed."""
        return timedelta(seconds=self.client.timeout_seconds) + LEASE_MARGIN

    def wants(self, event: Event) -> bool:
        return False  # its work comes from the full-text queue

    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None:
        return None

    async def aclose(self) -> None:
        await self.client.http.aclose()

    async def submission(
        self, conn: psycopg.AsyncConnection, paper: Paper, text: PaperText, requested: bool
    ) -> dict[str, Any]:
        """What the service is sent for the paper: its text, the topics the card is for and the
        extensions they ask for."""
        topics = await self._topics(conn, paper.id, requested)
        return {
            "paper": {
                "id": paper.id,
                "title": paper.title,
                "abstract": paper.abstract,
                "authors": paper.authors,
                "published": paper.published,
                "url": paper.url,
                "body": text.body,
                "body_source": text.source,
            },
            "extensions": sorted(
                {self.extensions[t.slug] for _, t in topics if t.slug in self.extensions}
            ),
            "selections": [
                {"name": name, "subject": t.subject, "rubric": t.rubric} for name, t in topics
            ],
        }

    async def _topics(
        self, conn: psycopg.AsyncConnection, paper_id: str, requested: bool
    ) -> list[tuple[str, Topic]]:
        """The topics the card is for, each with its class's name: the active Focus topics that
        grade the paper must read, else, for the owner's request, every active Focus topic."""
        grades = await word_store.focus_grades(conn, paper_id)
        tree = await taxonomy_store.load(conn)
        focus = [
            (tree.get(t.slug).name, t)
            for t in (tree.topics if tree else [])
            if t.depth == Depth.FOCUS and t.status == Status.ACTIVE
        ]
        chosen = [(n, t) for n, t in focus if grades.get(t.slug) == Grade.MUST_READ]
        return chosen or (focus if requested else [])


def load(name: str, options: dict[str, Any], config: Config) -> Deep:
    max_per_day = int(options.get("max_per_day", 10))
    if max_per_day < 0:
        raise IntegrationError("max_per_day cannot be negative")
    # The Deep worker reads each paper's text from the archive; without one it never starts,
    # and a card the owner asks for would wait for ever.
    try:
        required(enabled_using(config, "archive") or {}, "url", env="LITWATCH_ARCHIVE_URL")
        required_secret("LITWATCH_ARCHIVE_TOKEN")
    except IntegrationError as e:
        raise IntegrationError(f"deep reads papers from the archive: {e}") from e
    client = DeepClient(
        required(options, "url", env="LITWATCH_DEEP_URL"),
        poll_seconds=float(options.get("poll_seconds", 15.0)),
        timeout_seconds=float(options.get("timeout_minutes", 45)) * 60,
    )
    return Deep(client, name=name, extensions=options.get("extensions"), max_per_day=max_per_day)
