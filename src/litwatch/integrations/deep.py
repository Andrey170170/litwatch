"""deep: asks the `deep` service for a reading card on a paper, stores it, and emits
`card.ready`.

A card is made for a paper that is, when the event is handled, `must_read` for some Watch
topic and in the library (`grade.changed` to `must_read`, `paper.saved`), and for any paper
the owner asks about by hand (`card.requested`), whatever its grade or topic. A paper gets
one card; a stored card is never replaced by an automatic event.

litwatch owns all state; the service is a stateless job runner. It gets the paper's text,
the Watch topics that make the paper `must_read` (all active Watch topics for a request on
a paper that is `must_read` nowhere) and the card extensions those topics ask for, and
returns the validated card with a trace and token counts. A job that fails is recorded and
not retried by itself; a paper whose automatic attempts failed three times is left alone
until the owner asks. A service that cannot be reached is retried by the dispatcher. To
protect the subscription from a loose rubric, at most `max_per_day` automatic cards are
attempted in any 24 hours; a paper past the cap is passed over with the reason recorded, and
the owner can ask for its card. (Waiting instead would hold the owner's requests behind it.)

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
from litwatch.core import Event, Grade, Paper, TopicKind
from litwatch.integrations import IntegrationError, must_read_and_saved, required
from litwatch.integrations.archive import reader
from litwatch.integrations.papertext import BodyFetcher
from litwatch.store import integrations as state
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import word as word_store

log = logging.getLogger(__name__)

# Card extensions per Watch topic; the names are the deep service's EXTENSIONS.
EXTENSIONS = {"cot-faithfulness": "faithfulness", "cot-monitoring": "monitoring"}
MAX_AUTOMATIC_ATTEMPTS = 3


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

    async def process(
        self,
        paper: dict[str, Any],
        extensions: list[str] | None = None,
        selections: list[dict[str, Any]] | None = None,
    ) -> Outcome:
        """Runs one paper to completion. Progress is logged whenever the job advances."""
        resp = await self.http.post(
            f"{self.base_url}/process",
            json={"paper": paper, "extensions": extensions or [], "selections": selections or []},
        )
        resp.raise_for_status()
        job_id = resp.json()["id"]
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
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
                    paper["id"],
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
            if asyncio.get_running_loop().time() > deadline:
                return Outcome(None, [], stats, "", "litwatch gave up waiting for the deep job")


class Deep:
    name = "deep"
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
        paper_id = event.paper_id
        found = await state.card(conn, paper_id)
        if found and found["card"]:
            return
        requested = event.type == events.CARD_REQUESTED
        if not requested:
            if not await must_read_and_saved(conn, paper_id):
                return
            if found and found["attempts"] >= MAX_AUTOMATIC_ATTEMPTS:
                return
            since = datetime.now(UTC) - timedelta(days=1)
            if await state.cards_attempted_since(conn, since) >= self.max_per_day:
                reason = f"passed over: {self.max_per_day} automatic cards in the last 24 hours"
                await state.card_failed(conn, paper_id, reason, attempt=False)
                log.warning("%s: deep card %s", paper_id, reason)
                return
        paper = await papers_store.get(conn, paper_id)
        if paper is None:
            return
        topics = await self._topics(conn, paper_id, requested)
        body = await self._body(conn, paper)
        if body is None:
            await state.card_failed(conn, paper_id, "no paper text available")
            return
        outcome = await self.client.process(
            _paper(paper, *body),
            extensions=sorted(
                {self.extensions[t.slug] for t in topics if t.slug in self.extensions}
            ),
            selections=[{"name": t.name, "subject": t.subject, "rubric": t.rubric} for t in topics],
        )
        if outcome.error or outcome.result is None:
            error = outcome.error or "no result"
            await state.card_failed(conn, paper_id, error, outcome.stats, outcome.trace)
            log.warning("%s: deep card failed: %s", paper_id, error)
            return
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

    async def _topics(self, conn: psycopg.AsyncConnection, paper_id: str, requested: bool) -> list:
        grades = await word_store.effective_for(conn, paper_id)
        watch = [t for t in await topics_store.all(conn) if t.kind == TopicKind.WATCH]
        chosen = [t for t in watch if grades.get(t.slug) == Grade.MUST_READ]
        return chosen or (watch if requested else [])

    async def _body(self, conn: psycopg.AsyncConnection, paper: Paper) -> tuple[str, str] | None:
        if stored := await papers_store.body(conn, paper.id):
            return stored[0], stored[1] or "unknown"
        fetched = await self.fetcher.fetch(paper)
        if fetched is not None:
            await papers_store.set_body(conn, paper.id, *fetched)
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
