"""deep: asks the `deep` service for a reading card on a paper, stores it, and emits
`card.ready`.

A card is made for a paper that is, when the event is handled, `must_read` for some Focus
topic and in the library (`grade.changed` to `must_read`, `paper.saved`), and for any paper
the owner asks about by hand (`card.requested`), whatever its grade or topic. A paper gets
one card; a stored card is never replaced by an automatic event.

litwatch owns all state; the service is a stateless job runner that does not deduplicate.
It gets the paper's text, the Focus topics that make the paper `must_read` (all active Focus
topics for a request on a paper that is `must_read` nowhere) and the card extensions those
topics ask for, and returns the validated card with a trace and token counts.

Every submission that may reach the service is recorded as an attempt before it is sent, and
the job it starts is recorded once accepted. A failed poll or a restart then follows that
job rather than paying for another; a submission whose answer never came counts all the same.
A job that ends in failure is recorded and not retried by itself. A card whose job was
submitted for a revision or arXiv version the paper no longer has is not kept: the paper
waits again, first in line, as the same request or automatic card it was. A paper whose text cannot
be had yet (no HTML rendering, the archive still fetching the PDF, arXiv failing) waits: up to
three waiting papers are tried again before each event, and whenever the dispatcher finds
nothing to hand over, each at most hourly, for a day, after which the reason is recorded. The
event itself goes on, so one paper never holds up the rest. An owner's request for a paper
that was waiting by itself takes the wait over.
A paper gets at most three automatic submissions, and any one event at most three, a request
included; a paper stopped by that limit says so. At most `max_per_day` automatic submissions
are made in any 24 hours, to protect the subscription from a loose rubric; the owner's
requests do not count. A paper past the cap is passed over with the reason recorded, and the
owner can ask for its card. (Waiting instead would hold the owner's requests behind it.)

Options: `url` (or $LITWATCH_DEEP_URL), `extensions` ({topic slug: extension}), `max_per_day`
(10), `poll_seconds` (15) and `timeout_minutes` (45).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg

from litwatch import events
from litwatch.config import Config
from litwatch.core import Depth, Event, Grade, Paper, Status, Topic
from litwatch.integrations import IntegrationError, must_read_and_saved, required
from litwatch.integrations.archive import reader
from litwatch.integrations.papertext import BodyFetcher
from litwatch.store import integrations as state
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import word as word_store

log = logging.getLogger(__name__)

# Card extensions per Focus topic; the names are the deep service's EXTENSIONS.
EXTENSIONS = {"cot-faithfulness": "faithfulness", "cot-monitoring": "monitoring"}
MAX_AUTOMATIC_ATTEMPTS = 3  # automatic submissions per paper, ever
MAX_SUBMISSIONS = 3  # per event: a request, too, stops after this many unanswered submissions
# A paper whose text could not be had is tried again, while other events are handled: this
# many per event, each at most once in this long, until it has waited this long.
WAIT_RETRIES_PER_EVENT = 3
WAIT_RETRY_EVERY = timedelta(hours=1)
WAIT_AT_MOST = timedelta(days=1)
WAITING = "waiting"


@dataclass
class Outcome:
    result: dict[str, Any] | None
    trace: list[dict[str, Any]]
    stats: dict[str, Any]
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

    async def submit(
        self,
        paper: dict[str, Any],
        extensions: list[str] | None = None,
        selections: list[dict[str, Any]] | None = None,
    ) -> str:
        """Submit one paper; returns the job's id. The service does not deduplicate, so a
        caller records the attempt before and the job after, and never submits twice."""
        resp = await self.http.post(
            f"{self.base_url}/process",
            json={"paper": paper, "extensions": extensions or [], "selections": selections or []},
        )
        resp.raise_for_status()
        return str(resp.json()["id"])

    async def follow(self, job_id: str, since: datetime, label: str = "") -> Outcome:
        """Poll a job until it ends, or until `timeout_seconds` after `since` (when it was
        submitted). A failed poll raises, and the job can be followed again later."""
        deadline = since + timedelta(seconds=self.timeout_seconds)
        seen = ""
        while True:
            await asyncio.sleep(self.poll_seconds)
            resp = await self.http.get(f"{self.base_url}/jobs/{job_id}")
            if resp.status_code == 404:
                return Outcome(None, [], {}, "", "deep job vanished (service restarted?)")
            resp.raise_for_status()
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
                    stats,
                    job.get("model") or "",
                    job.get("error"),
                )
            if datetime.now(UTC) > deadline:
                return Outcome(None, [], stats, "", "litwatch gave up waiting for the deep job")


class Deep:
    name = "deep"
    makes_cards = True  # it answers `card.requested`
    description = (
        "Asks the deep service for a reading card on every must-read library paper, and on "
        "any paper the owner asks about."
    )

    def __init__(
        self,
        client: DeepClient,
        fetcher: BodyFetcher,
        *,
        name: str = "deep",
        extensions: dict[str, str] | None = None,
        max_per_day: int = 10,
    ) -> None:
        self.name = name
        self.client = client
        self.fetcher = fetcher
        self.extensions = EXTENSIONS if extensions is None else extensions
        self.max_per_day = max_per_day

    def wants(self, event: Event) -> bool:
        if event.type in (events.PAPER_SAVED, events.CARD_REQUESTED):
            return True
        return event.type == events.GRADE_CHANGED and event.payload.get("after") == "must_read"

    async def aclose(self) -> None:
        await self.client.http.aclose()
        await self.fetcher.aclose()

    async def handle(self, conn: psycopg.AsyncConnection, event: Event) -> None:
        await self._retry_waiting(conn)
        automatic = event.type != events.CARD_REQUESTED
        if await self._make(conn, event.paper_id, event.id, automatic) != WAITING:
            await state.stop_waiting(conn, event.paper_id)

    async def idle(self, conn: psycopg.AsyncConnection) -> None:
        """With no event to handle, papers waiting for their text are still tried again and,
        after a day, given up."""
        await self._retry_waiting(conn)

    async def _retry_waiting(self, conn: psycopg.AsyncConnection) -> None:
        """Try again, a few at a time, for papers whose text was not to be had. Their trouble
        is their own: a failure here is logged and never fails the event in hand."""
        now = datetime.now(UTC)
        for row in await state.waiting(conn, now - WAIT_RETRY_EVERY, WAIT_RETRIES_PER_EVENT):
            paper_id = row["paper_id"]
            if now - row["since"] > WAIT_AT_MOST:
                await state.stop_waiting(conn, paper_id)
                await state.card_failed(conn, paper_id, "no paper text came within a day")
                continue
            try:
                outcome = await self._make(conn, paper_id, row["event_id"], row["automatic"])
            except Exception:
                log.exception("%s: retrying the deep card failed", paper_id)
                await state.touch_waiting(conn, paper_id)
                continue
            if outcome != WAITING:
                await state.stop_waiting(conn, paper_id)

    async def _make(
        self, conn: psycopg.AsyncConnection, paper_id: str, event_id: int, automatic: bool
    ) -> str | None:
        """Get the paper its card, if it should have one now. Returns `WAITING` when its text
        could not be had, which is recorded so it is tried again later."""
        found = await state.card(conn, paper_id)
        if found and found["card"]:
            return None
        if found and found["job_id"]:
            # Submitted before a failure or a restart: follow that job, never pay for another.
            return await self._follow(conn, paper_id, found)
        if automatic:
            if not await must_read_and_saved(conn, paper_id):
                return None
            if await state.attempts(conn, paper_id, automatic=True) >= MAX_AUTOMATIC_ATTEMPTS:
                reason = (
                    f"stopped: {MAX_AUTOMATIC_ATTEMPTS} automatic submissions came to nothing; "
                    "ask for the card to try again"
                )
                await state.card_failed(conn, paper_id, reason)
                return None
            since = datetime.now(UTC) - timedelta(days=1)
            if await state.automatic_attempts_since(conn, since) >= self.max_per_day:
                reason = f"passed over: {self.max_per_day} automatic cards in the last 24 hours"
                await state.card_failed(conn, paper_id, reason)
                log.warning("%s: deep card %s", paper_id, reason)
                return None
        if await state.attempts(conn, paper_id, event_id=event_id) >= MAX_SUBMISSIONS:
            await state.card_failed(
                conn, paper_id, f"gave up after {MAX_SUBMISSIONS} submissions went unanswered"
            )
            return None
        paper = await papers_store.get(conn, paper_id)
        if paper is None:
            return None
        topics = await self._topics(conn, paper_id, not automatic)
        body = await self._body(conn, paper)
        if body is None:
            await state.wait_for_text(conn, paper_id, event_id, automatic)
            await state.card_failed(conn, paper_id, "waiting for the paper's text")
            return WAITING
        # Counted before it is sent: the service may accept a job whose answer never arrives.
        await state.add_attempt(conn, paper_id, event_id, automatic)
        job_id = await self.client.submit(
            _paper(paper, *body),
            extensions=sorted(
                {self.extensions[t.slug] for _, t in topics if t.slug in self.extensions}
            ),
            selections=[
                {"name": name, "subject": t.subject, "rubric": t.rubric} for name, t in topics
            ],
        )
        await state.job_started(conn, paper, job_id, event_id, automatic)
        return await self._follow(conn, paper_id, await state.card(conn, paper_id))

    async def _follow(
        self, conn: psycopg.AsyncConnection, paper_id: str, job: dict[str, Any]
    ) -> str | None:
        """Follow the job to its end and keep its card, if the card is still of the paper as
        it is: one made from text that intake has since rewritten or replaced with a new
        version is dropped, and the paper waits to be tried again, first in line."""
        outcome = await self.client.follow(job["job_id"], job["job_since"], label=paper_id)
        if outcome.error or outcome.result is None:
            error = outcome.error or "no result"
            await state.card_failed(conn, paper_id, error, outcome.stats, outcome.trace)
            log.warning("%s: deep card failed: %s", paper_id, error)
            return None
        paper = await papers_store.get(conn, paper_id)
        if paper is not None and (paper.revision, paper.version) != (
            job["job_revision"],
            job["job_version"],
        ):
            await state.card_failed(
                conn, paper_id, "the paper changed while its card was made; making it again"
            )
            await state.wait_for_text(
                conn, paper_id, job["job_event"], bool(job["job_automatic"]), due=True
            )
            log.info("%s: deep card dropped: made for text the paper no longer has", paper_id)
            return WAITING
        async with conn.transaction():
            await state.put_card(
                conn, paper_id, outcome.result, outcome.model, outcome.stats, outcome.trace
            )
            await events.emit(conn, events.CARD_READY, paper_id, model=outcome.model)
        log.info(
            "%s: deep card in %s cells, list cost $%.2f",
            paper_id,
            outcome.stats.get("cells"),
            outcome.stats.get("list_cost", 0.0),
        )
        return None

    async def _topics(
        self, conn: psycopg.AsyncConnection, paper_id: str, requested: bool
    ) -> list[tuple[str, Topic]]:
        """The topics the card is for, each with its class's name."""
        grades = await word_store.effective_for(conn, paper_id)
        tree = await taxonomy_store.load(conn)
        focus = [
            (tree.get(t.slug).name, t)
            for t in (tree.topics if tree else [])
            if t.depth == Depth.FOCUS and t.status == Status.ACTIVE
        ]
        chosen = [(n, t) for n, t in focus if grades.get(t.slug) == Grade.MUST_READ]
        return chosen or (focus if requested else [])

    async def _body(self, conn: psycopg.AsyncConnection, paper: Paper) -> tuple[str, str] | None:
        if stored := await papers_store.body(conn, paper.id):
            return stored[0], stored[1] or "unknown"
        fetched = await self.fetcher.fetch(paper)
        if fetched is None:
            return None
        if not await papers_store.set_body(conn, paper, *fetched):
            # Revised while the text was fetched: that text is of an old version or wording.
            # The paper waits, and the next try fetches the text as it now is.
            log.info("%s: revised while its text was fetched; fetching again later", paper.id)
            return None
        log.info("%s: stored %s text (%d chars)", paper.id, fetched[1], len(fetched[0]))
        return fetched


def _paper(paper: Paper, body: str, source: str) -> dict[str, Any]:
    return {
        "id": paper.id,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": paper.authors,
        "published": paper.published,
        "url": paper.url,
        "body": body,
        "body_source": source,
    }


def load(name: str, options: dict[str, Any], config: Config) -> Deep:
    max_per_day = int(options.get("max_per_day", 10))
    if max_per_day < 0:
        raise IntegrationError("max_per_day cannot be negative")
    client = DeepClient(
        required(options, "url", env="LITWATCH_DEEP_URL"),
        poll_seconds=float(options.get("poll_seconds", 15.0)),
        timeout_seconds=float(options.get("timeout_minutes", 45)) * 60,
    )
    return Deep(
        client,
        BodyFetcher(archive=reader(config)),
        name=name,
        extensions=options.get("extensions"),
        max_per_day=max_per_day,
    )
