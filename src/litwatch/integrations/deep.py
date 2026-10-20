"""deep: the deep service's Deep cards (docs/specs/v1/integrations.md). Which papers get a card,
and when, is the full-text queue's (`litwatch.fulltext.deep`): a card is a pass like a Brief,
so it waits for the paper's text and is re-checked under the paper's lock before it runs. This
module is how one is asked for: what a submission carries (the paper's text from the archive,
the topics the card is for and the card extensions they ask for), and the service's job
protocol, followed to the job's end with its usage.

The queue records each submission as an attempt before it is sent and the job once accepted,
and sends the attempt as the submission's key, by which the service finds the job it
started for that key while it keeps it; so a submission whose answer was lost is looked up,
never sent again. A failed job says whose failure it was, the paper's or the service's. It
reacts to no event: `card.requested` is the owner's request already queued, and `card.ready` is
emitted when the card is stored.

Options: `url` (or $LITWATCH_DEEP_URL), `extensions` ({topic slug: extension}), `poll_seconds`
(15) and `timeout_minutes` (45). There is no daily cap: `max_per_day` is refused.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

import httpx
import psycopg

from litwatch.archive import PaperText
from litwatch.config import Config
from litwatch.core import Depth, Event, Grade, Paper, Status, Topic
from litwatch.integrations import IntegrationError, enabled_using, required, required_secret
from litwatch.store import fulltext as fulltext_store
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
    list cost); None when it never reported (the job was lost, or litwatch gave up waiting).
    `failure` says whose a failed job's failure was, as the service tagged it, or the service's
    when the job was lost or never ended."""

    result: dict[str, Any] | None
    trace: list[dict[str, Any]]
    usage: dict[str, Any] | None
    model: str
    error: str | None = None
    failure: Literal["paper", "service"] | None = None


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

    async def health(self) -> None:
        """Raise unless the service answers its health check."""
        resp = await self.http.get(f"{self.base_url}/healthz", timeout=10.0)
        resp.raise_for_status()

    async def submit(self, submission: dict[str, Any], *, key: str) -> str:
        """Submit one paper; returns the job's id. `key` names the submission: the service
        answers a key it has with that key's job, so one sent again after a lost answer never
        starts a second."""
        resp = await self.http.post(
            f"{self.base_url}/process", json=submission, headers={"Idempotency-Key": key}
        )
        resp.raise_for_status()
        return str(resp.json()["id"])

    async def job_for(self, key: str) -> str | None:
        """The job a submission with `key` started, if the service still has it."""
        resp = await self.http.get(f"{self.base_url}/keys/{quote(key, safe='')}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return str(resp.json()["id"])

    async def follow(self, job_id: str, since: datetime, label: str = "") -> Outcome:
        """Poll a job until it ends, or until `timeout_seconds` after `since` (when it was
        submitted). A failed poll raises, and the job can be followed again later, until then."""
        deadline = since + timedelta(seconds=self.timeout_seconds)
        gave_up = Outcome(
            None, [], None, "", "litwatch gave up waiting for the deep job", "service"
        )
        seen = ""
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                resp = await self.http.get(f"{self.base_url}/jobs/{job_id}")
                if resp.status_code == 404:
                    error = "deep job vanished (service restarted?)"
                    return Outcome(None, [], None, "", error, "service")
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
                    job.get("failure"),
                )
            if datetime.now(UTC) > deadline:
                return gave_up  # the job may go on spending; what it spent was never reported


class Deep:
    name = "deep"
    description = (
        "Writes a Deep card for every Focus must read and Area pick, unless unsaved, and for "
        "any paper the owner asks about, through the full-text queue."
    )

    def __init__(
        self,
        client: DeepClient,
        *,
        name: str = "deep",
        extensions: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.client = client
        self.extensions = EXTENSIONS if extensions is None else extensions

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
        grade the paper must read and the active Area topics whose published digests picked it,
        else, for the owner's request, every active Focus topic."""
        grades = await word_store.focus_grades(conn, paper_id)
        found = await fulltext_store.deep_facts(conn, paper_id)
        picked = found.picked_by if found else frozenset()
        tree = await taxonomy_store.load(conn)
        active = [
            (tree.get(t.slug).name, t)
            for t in (tree.topics if tree else [])
            if t.status == Status.ACTIVE
        ]
        chosen = [
            (n, t)
            for n, t in active
            if (t.depth == Depth.FOCUS and grades.get(t.slug) == Grade.MUST_READ)
            or (t.depth == Depth.AREA and t.slug in picked)
        ]
        focus = [(n, t) for n, t in active if t.depth == Depth.FOCUS]
        return chosen or (focus if requested else [])


def load(name: str, options: dict[str, Any], config: Config) -> Deep:
    # A cap the owner set is not dropped without a word.
    if "max_per_day" in options:
        raise IntegrationError("max_per_day is gone: Deep cards have no daily cap; remove it")
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
    return Deep(client, name=name, extensions=options.get("extensions"))
