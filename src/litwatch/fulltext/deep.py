"""Deep card passes (docs/specs/v1/pipeline.md, section 6; integrations.md). `word.settle` queues
an automatic one for a saved Focus must read with no card, and `request` the owner's, which no
settle drops. They are worked by their own worker, one at a time, since a card takes up to most
of an hour: a claimed pass is settled under its paper's lock before anything is spent
(`still_wanted`), its text is read from the archive at urgent priority, and the `deep`
integration's service is given it.

Each submission is an attempt, recorded before it is sent and given its job once accepted, so a
worker that dies while a job runs leaves the job to the next claim to follow, never paid for
twice. When a job ends, with a card or failed, the attempt records the job's usage, or that it
is unknown (the job was lost before it reported, the submission's answer never came). A
failed job fails the pass: it is tried again on the next nightly run, and after `FAILED_RUNS`
failed runs it waits for the owner on Quality, as every pass does. At most `max_per_day`
automatic submissions are made in any 24 hours; the owner's requests do not count, and go first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
import psycopg

from litwatch import events, store
from litwatch.archive import ArchiveClient
from litwatch.core.fulltext import REQUESTED, TextRead
from litwatch.feedback import word
from litwatch.fulltext import TICK_SECONDS, read, record, still_wanted
from litwatch.integrations.deep import Deep
from litwatch.store import fulltext as store_
from litwatch.store import integrations as cards
from litwatch.store.fulltext import Claimed

log = logging.getLogger(__name__)


async def request(conn: psycopg.AsyncConnection, paper_id: str) -> int | None:
    """The owner asks for a Deep card on the paper, whatever its grade, a card already made
    included ("Write again"): their request takes over the open Deep pass or queues one, with
    `card.requested`, under the paper's lock. Returns the pass's id; None for a paper that is
    not there or dismissed, whose passes are never worked."""
    async with conn.transaction():
        await word.lock(conn, paper_id)
        found = await store_.deep_facts(conn, paper_id)
        if found is None or found.dismissed:
            return None
        pass_id = await store_.request_card(conn, paper_id, found.revision)
        await events.emit(conn, events.CARD_REQUESTED, paper_id, pass_id=pass_id)
    return pass_id


async def work(
    conn: psycopg.AsyncConnection, archive: ArchiveClient, deep: Deep, *, limit: int | None = None
) -> Counter[str]:
    """Work the Deep passes that are due, one at a time, up to `limit` claimed. Returns how the
    tries ended."""
    counts: Counter[str] = Counter()
    tried: set[int] = set()
    while limit is None or len(tried) < limit:
        day_ago = datetime.now(UTC) - timedelta(days=1)
        automatic = await cards.automatic_attempts_since(conn, day_ago) < deep.max_per_day
        claimed = await store_.claim(conn, deep.lease, 1, tried, deep=True, automatic=automatic)
        if not claimed:
            break
        (c,) = claimed
        tried.add(c.id)
        # A pass with an attempt open has started: its job is followed whatever the paper wants.
        attempt = await cards.open_attempt(conn, c.id)
        why = None if attempt else await still_wanted(conn, c)
        if why is None:
            why = await _work_one(conn, archive, deep, c, attempt)
        if why != "lost":
            counts[why] += 1
    return counts


async def _work_one(
    conn: psycopg.AsyncConnection,
    archive: ArchiveClient,
    deep: Deep,
    c: Claimed,
    attempt: dict | None,
) -> str:
    """Follow the pass's job, one left running by a failed poll or a dead worker, or read the
    paper's text and submit it; then record how the job ended."""
    if attempt is not None and attempt["job_id"] is None:
        # Sent, but its answer never came before the worker died: it may have run.
        return await _ended(conn, c, attempt["id"], None, "the submission's answer never came")
    if attempt is None:
        a = await read(archive, c)
        if a.text is None:  # awaiting text, or none to be had
            await record(conn, a)
            return "awaiting_text" if a.waiting else "card_failed"
        text = TextRead(a.text.version, a.text.source, a.text.sha256)
        async with conn.transaction():
            # Under the paper's lock: the owner's request may have taken the pass over since
            # its claim, and then the submission and its attempt are theirs.
            await word.lock(conn, c.paper.id)
            reason = await store_.submitted(conn, c, text)
            if reason is None:
                return "lost"
            requested = reason == REQUESTED
            submission = await deep.submission(conn, c.paper, a.text, requested)
            attempt_id = await cards.add_attempt(conn, c.paper.id, c.id, automatic=not requested)
        try:
            job_id = await deep.client.submit(submission)
        except httpx.HTTPError as exc:
            # The service may have taken it all the same: it counts, its usage unknown.
            return await _ended(conn, c, attempt_id, None, f"submitting failed: {exc}")
        await cards.job_started(conn, attempt_id, job_id)
        attempt = {"id": attempt_id, "job_id": job_id, "at": datetime.now(UTC)}
    try:
        outcome = await deep.client.follow(attempt["job_id"], attempt["at"], label=c.paper.id)
    except httpx.HTTPError as exc:
        # The job goes on: the pass is let go, still running, and its next claim follows it.
        log.warning("%s: following the deep job failed: %s", c.paper.id, exc)
        await store_.let_go(conn, c, f"following the deep job: {exc}")
        return "card_following"
    if outcome.error or outcome.result is None:
        return await _ended(
            conn, c, attempt["id"], outcome.usage, outcome.error or "no card", outcome.trace
        )
    async with conn.transaction():
        await word.lock(conn, c.paper.id)
        if not await store_.held(conn, c):
            return "lost"
        revision, text = await store_.read_of(conn, c.id)
        assert text is not None  # recorded before the job was submitted
        await cards.attempt_ended(conn, attempt["id"], outcome.usage, None)
        await cards.put_card(
            conn,
            c.paper.id,
            outcome.result,
            outcome.model,
            outcome.usage or {},
            outcome.trace,
            revision=revision,
            version=text.version,
            source=text.source,
            pass_id=c.id,
        )
        await store_.done(conn, c, text)
        await events.emit(conn, events.CARD_READY, c.paper.id, model=outcome.model, pass_id=c.id)
    usage = outcome.usage or {}
    log.info(
        "%s: deep card in %s turns, list cost $%.2f",
        c.paper.id,
        usage.get("turns"),
        usage.get("list_cost", 0.0),
    )
    return "cards_written"


async def _ended(
    conn: psycopg.AsyncConnection,
    c: Claimed,
    attempt_id: int,
    usage: dict | None,
    error: str,
    trace: list | None = None,
) -> str:
    """An attempt that made no card: its usage (None, unknown) and the error, and the pass
    failed for this run, a run that counts."""
    log.warning("%s: deep card failed: %s", c.paper.id, error)
    async with conn.transaction():
        await word.lock(conn, c.paper.id)
        await cards.attempt_ended(conn, attempt_id, usage, error)
        if not await store_.held(conn, c):
            return "lost"
        await cards.card_failed(conn, c.paper.id, error, usage, trace)
        revision, text = await store_.read_of(conn, c.id)
        await store_.failed(conn, c, error, counts=True, text=text)
        await word.settle(conn, c.paper.id)
    return "card_failed"


async def run_forever(
    dsn: str, archive: ArchiveClient, deep: Deep, wake: asyncio.Event | None = None
) -> None:
    """The service's Deep worker: work the Deep passes every `TICK_SECONDS`, or as soon as
    `wake` is set (the owner asked for a card), until cancelled. Each tick opens its own
    connection, so a lost database is retried rather than fatal."""
    wake = wake or asyncio.Event()
    log.info("deep card worker on")
    while True:
        try:
            async with await store.connect(dsn) as conn:
                if done := await work(conn, archive, deep):
                    log.info("deep cards: %s", dict(done))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("deep card tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), TICK_SECONDS)
        wake.clear()
