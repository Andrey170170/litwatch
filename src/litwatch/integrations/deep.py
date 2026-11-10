"""deep: the deep service's Deep reads, and the Deep cards before them (docs/specs/deep-read.md;
docs/specs/v1/integrations.md). Which papers get one, and when, is the full-text queue's
(`litwatch.fulltext.deep`): a card is a pass like a Brief, so it waits for the paper's text and
is re-checked under the paper's lock before it runs. This module is how one is asked for: what
a submission carries (the paper's text from the archive and its arXiv version, the topics the
card is for and the card extensions they ask for), and the service's job protocol, followed to
the job's end with its usage. What a finished job returns is checked before any of it is
stored (`core.deepread.received`): a result of an unknown format, or figures or reviews that
are not what docs/integrations.md says, end the job as the service's failure.

The queue records each submission as an attempt before it is sent and the job once accepted,
and sends the attempt as the submission's key, by which the service finds the job it
started for that key while it keeps it; so a submission whose answer was lost is looked up,
never sent again. A failed job says whose failure it was, the paper's or the service's. It
reacts to no event: `card.requested` is the owner's request already queued, and `card.ready` is
emitted when the card is stored.

Options: `url` (or $LITWATCH_DEEP_URL), `extensions` ({topic slug: extension}), `poll_seconds`
(15) and `timeout_minutes` (135). There is no daily cap: `max_per_day` is refused. Keep
`timeout_minutes` above the service's `DEEP_WALL_CLOCK_MIN` (120, the time a read's writing,
review and revisions may take): a job running longer than it, by its own start, ends as stuck,
so a lower one ends healthy jobs and lets a duplicate in. The 15 minutes between the two are
the service's to end its own run and for litwatch's next poll to see it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

import httpx
import psycopg

from litwatch.archive import PaperText
from litwatch.config import Config
from litwatch.core import Event, Grade, Paper, Status, Topic, deepread
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
    list cost), and `trace` what the job did; each None when it never reported (the job was
    lost, or litwatch gave up waiting). `figures` are the result's, decoded, and `reviews` the
    job's review rounds as the service returned them (None: no review ran).
    `failure` says whose a failed job's failure was, as the service tagged it, or the service's
    when the job was lost or never ended. `still_running`: litwatch stopped waiting at the
    job's deadline, but the job may yet run: followed again after each pause until it ends,
    or until it has run past the deadline by the service's own clock (`DeepClient.follow`)."""

    result: dict[str, Any] | None
    trace: list[dict[str, Any]] | None
    usage: dict[str, Any] | None
    model: str
    error: str | None = None
    failure: Literal["paper", "service"] | None = None
    still_running: bool = False
    figures: tuple[deepread.Figure, ...] = ()
    reviews: list[Any] | None = None


def ended(job: dict[str, Any]) -> Outcome:
    """A finished job as the service reports it. One that says it is done is checked before
    its result is taken (`deepread.received`): what litwatch cannot store is the service's
    failure, an error with the job's usage and trace kept, never a card with something
    missing."""
    said = job.get("reviews")
    outcome = Outcome(
        job.get("result"),
        job.get("trace"),
        job.get("stats"),
        job.get("model") or "",
        job.get("error"),
        job.get("failure"),
        reviews=said if isinstance(said, list) else None,
    )
    if outcome.error or outcome.result is None:
        return outcome
    try:
        figures, reviews = deepread.received(outcome.result, job.get("figures"), said)
    except ValueError as exc:
        error = f"the deep service returned a result litwatch cannot store: {exc}"
        return replace(outcome, result=None, error=error, failure="service")
    return replace(outcome, figures=tuple(figures), reviews=reviews)


# A 4xx the service answers a submission with that is not about the submission: a wrong
# address (404), its own timeout (408) or load (429). Any other 4xx turns the submission down
# for what it is (400, 413, 422 today), and it would be turned down again.
NOT_REJECTIONS = frozenset({404, 408, 429})


class RejectedError(Exception):
    """The service turned a submission down for what it is, in its own words; no job started."""


def _said(resp: httpx.Response) -> str:
    """What the service said in its error answer (`{"error": ...}`)."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    return str(body.get("error", body)) if isinstance(body, dict) else str(body)


class DeepClient:
    """HTTP client for the deep service: submit a job, follow its progress, collect it."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient | None = None,
        poll_seconds: float = 15.0,
        timeout_seconds: float = 135 * 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=60.0)
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    async def health(self) -> dict[str, Any]:
        """The service's health check, its sign-in in `auth` (`state`, and `detail` when not
        ok); raises unless it answers."""
        resp = await self.http.get(f"{self.base_url}/healthz", timeout=10.0)
        resp.raise_for_status()
        return resp.json()

    async def submit(self, submission: dict[str, Any], *, key: str) -> str:
        """Submit one paper; returns the job's id. `key` names the submission: the service
        answers a key it has with that key's job, so one sent again after a lost answer never
        starts a second. Raises `RejectedError` when the service turns it down for what it is."""
        resp = await self.http.post(
            f"{self.base_url}/process", json=submission, headers={"Idempotency-Key": key}
        )
        if 400 <= resp.status_code < 500 and resp.status_code not in NOT_REJECTIONS:
            raise RejectedError(f"{resp.status_code} {_said(resp)}")
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
        submitted). A failed poll raises, and the job can be followed again later, until then.
        Past the deadline the job is `still_running`, to be followed again later (one poll),
        unless it has been running longer than `timeout_seconds` by the service's own clock,
        whose wall clock is shorter: the service is stuck, and litwatch gives up on the job. A
        job queued behind another, or unreachable, may yet run, so it is never given up on."""
        deadline = since + self.limit
        error = "litwatch stopped waiting for the deep job, to follow it again"
        kept = Outcome(None, None, None, "", error, "service", still_running=True)
        seen = ""
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                resp = await self.http.get(f"{self.base_url}/jobs/{job_id}")
                if resp.status_code == 404:
                    error = "deep job vanished (service restarted?)"
                    return Outcome(None, None, None, "", error, "service")
                resp.raise_for_status()
            except httpx.HTTPError:
                if datetime.now(UTC) > deadline:
                    return kept
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
                return ended(job)
            if datetime.now(UTC) > deadline:
                started = job.get("started_at")
                running = started and datetime.now(UTC) - datetime.fromisoformat(started)
                if job["status"] == "running" and running and running > self.limit:
                    # It may go on spending; what it spent was never reported.
                    error = "litwatch gave up on the deep job: running past its deadline"
                    return Outcome(None, None, None, "", error, "service")
                return kept

    @property
    def limit(self) -> timedelta:
        return timedelta(seconds=self.timeout_seconds)


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
        self, conn: psycopg.AsyncConnection, paper: Paper, text: PaperText
    ) -> dict[str, Any]:
        """What the service is sent for the paper: its text and the arXiv version it is of (null
        when the archive cannot say), which the service fetches the paper's figures at; the
        topics the card is for with the grade each gave it, and the extensions they ask for."""
        topics = await self._topics(conn, paper.id)
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
                "body_version": text.version,
            },
            "extensions": sorted(
                {self.extensions[t.slug] for _, t, _ in topics if t.slug in self.extensions}
            ),
            "selections": [
                {"name": name, "subject": t.subject, "rubric": t.rubric, "grade": g.value}
                for name, t, g in topics
            ],
        }

    async def _topics(
        self, conn: psycopg.AsyncConnection, paper_id: str
    ) -> list[tuple[str, Topic, Grade]]:
        """The topics the card is for: the active topics, at any depth, that grade the paper
        above none, each with its class's name and its grade (docs/specs/deep-read.md,
        "Lenses"). An Area pick asks for a card but is a topic of it only through its grade; a
        paper no topic grades gets a card written for none."""
        grades = await word_store.effective_for(conn, paper_id)
        tree = await taxonomy_store.load(conn)
        return [
            (tree.get(t.slug).name, t, grades[t.slug].grade)
            for t in (tree.topics if tree else [])
            if t.status == Status.ACTIVE and t.slug in grades and grades[t.slug].grade != Grade.NONE
        ]


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
        timeout_seconds=float(options.get("timeout_minutes", 135)) * 60,
    )
    return Deep(client, name=name, extensions=options.get("extensions"))
