"""Deep card passes (docs/specs/v1/pipeline.md, section 6; integrations.md). `word.settle` queues
an automatic one for a Focus must read or a published Area pick with no card, unless it is
unsaved, and `request` the owner's, which no settle drops. They are worked by their own worker,
one at a time, since a card takes up to most of an hour: a claimed pass is settled under its
paper's lock before anything is spent (`still_wanted`), its text is read from the archive at
urgent priority, and the `deep` integration's service is given it.

Each submission is an attempt, recorded before it is sent and given its job once accepted, so a
worker that dies while a job runs leaves the job to the next claim to follow, never paid for
twice. A submission carries its attempt as a key, which the service answers with the job it
already started for it, so one whose answer never came is sent again with the same key. When a
job ends, with a card or failed, the attempt records the job's usage, or that it is unknown
(the job was lost before it reported), and whose failure it was.

A job that failed because of the paper (no card within its budget, an invalid card) fails the
pass: it is tried again on the next nightly run, and after `FAILED_RUNS` failed runs it waits
for the owner on Quality, as every pass does. One that failed because of the service (the
service tagged it so, lost it, or never ended it) counts against nothing: the pass goes back in
the queue. Nor does an error submitting or following a job, after which the attempt and the
pass stay as they are, to be followed or sent again. Any service failure pauses the whole
queue for `PAUSE`. There is no daily cap; the owner's requests go first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import psycopg

from litwatch import events, store
from litwatch.archive import ArchiveClient
from litwatch.core import Cause
from litwatch.core.fulltext import REQUESTED, TextRead
from litwatch.feedback import word
from litwatch.fulltext import TICK_SECONDS, judged, read, record, still_wanted
from litwatch.integrations.deep import Deep
from litwatch.store import fulltext as store_
from litwatch.store import integrations as cards
from litwatch.store.fulltext import Claimed

log = logging.getLogger(__name__)

# After a service failure the queue waits this long before its next try.
PAUSE = timedelta(minutes=15)


async def request(conn: psycopg.AsyncConnection, paper_id: str) -> int | None:
    """The owner asks for a Deep card on the paper, whatever its grade, a card already made
    included ("Write again"): their request takes over the open Deep pass or queues one, with
    `card.requested`, under the paper's lock. Returns the pass's id; None for a paper that is
    not there or dismissed, whose passes are never worked."""
    async with word.change(conn, paper_id, cause=Cause.OWNER):
        found = await store_.deep_facts(conn, paper_id)
        if found is None or found.dismissed:
            return None
        pass_id = await store_.request_card(conn, paper_id, found.revision)
        await events.emit(conn, events.CARD_REQUESTED, paper_id, pass_id=pass_id)
    return pass_id


async def work(
    conn: psycopg.AsyncConnection, archive: ArchiveClient, deep: Deep, *, limit: int | None = None
) -> Counter[str]:
    """Work the Deep passes that are due, one at a time, up to `limit` claimed, unless another
    worker is working them. Returns how the tries ended."""
    async with store_.deep_worker(conn) as mine:
        if not mine:
            log.info("another worker is working the Deep passes")
            return Counter()
        return await _work(conn, archive, deep, limit)


async def _work(
    conn: psycopg.AsyncConnection, archive: ArchiveClient, deep: Deep, limit: int | None
) -> Counter[str]:
    counts: Counter[str] = Counter()
    tried: set[int] = set()
    while limit is None or len(tried) < limit:
        if until := await cards.paused_until(conn, PAUSE):
            log.debug("the deep queue waits after a service failure, until %s", until)
            break
        claimed = await store_.claim(conn, deep.lease, 1, tried, deep=True)
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
    paper's text and submit it, sending again one whose answer never came; then record how the
    job ended."""
    if attempt is None or attempt["job_id"] is None:
        a = await read(archive, c)
        if a.text is None and attempt is not None:
            # Its submission may have started a job: the attempt stays, and the pass running.
            await store_.let_go(conn, c, a.error or "awaiting its text to send again")
            return "awaiting_text"
        if a.text is None:  # awaiting text, or none to be had
            await record(conn, a)
            return "awaiting_text" if a.waiting else "card_failed"
        if attempt is None:
            text = TextRead(a.text.version, a.text.source, a.text.sha256)
            async with word.change(conn, c.paper.id, cause=Cause.VERDICT):
                # Under the paper's lock, judged again as at its claim: the paper may have been
                # dismissed or unsaved during the read, and nothing is spent then. The owner's
                # request may have taken the pass over since its claim, and then the submission
                # and its attempt are theirs.
                if why := await judged(conn, c):
                    return why
                reason = await store_.submitted(conn, c, text)
                if reason is None:
                    return "lost"
                requested = reason == REQUESTED
                submission = await deep.submission(conn, c.paper, a.text, requested)
                attempt_id = await cards.add_attempt(
                    conn, c.paper.id, c.id, automatic=not requested
                )
        else:
            # Sent before, its answer lost: the same attempt again, whatever the paper wants by
            # now, as the job it may have started is followed whatever the paper wants.
            attempt_id = attempt["id"]
            requested = not attempt["automatic"]
            submission = await deep.submission(conn, c.paper, a.text, requested)
        try:
            job_id = await deep.client.submit(submission, key=f"{attempt_id}:{c.paper.id}")
        except httpx.HTTPError as exc:
            # The service may have taken it all the same: sent again with its key after the pause.
            return await _service_failed(conn, c, attempt_id, f"submitting failed: {exc}")
        await cards.job_started(conn, attempt_id, job_id)
        attempt = {"id": attempt_id, "job_id": job_id, "at": datetime.now(UTC)}
    try:
        outcome = await deep.client.follow(attempt["job_id"], attempt["at"], label=c.paper.id)
    except httpx.HTTPError as exc:
        # The job goes on: followed again by the pass's next claim, after the pause.
        return await _service_failed(conn, c, attempt["id"], f"following the deep job: {exc}")
    if outcome.error or outcome.result is None:
        return await _ended(
            conn,
            c,
            attempt["id"],
            outcome.usage,
            outcome.error or "no card",
            "service" if outcome.failure == "service" else "paper",
            outcome.trace,
        )
    async with word.change(conn, c.paper.id, cause=Cause.VERDICT):
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
    failure: Literal["paper", "service"],
    trace: list | None = None,
) -> str:
    """An attempt that made no card: its usage (None, unknown), the error and whose `failure` it
    was. The paper's fails the pass for this run, a run that counts; the service's puts it back
    in the queue, which pauses."""
    log.warning("%s: deep card failed (%s): %s", c.paper.id, failure, error)
    async with word.change(conn, c.paper.id, cause=Cause.VERDICT):
        await cards.attempt_ended(conn, attempt_id, usage, error, failure)
        if not await store_.held(conn, c):
            return "lost"
        await cards.card_failed(conn, c.paper.id, error, usage, trace)
        if failure == "service":
            await store_.wait_again(conn, c, error)
            return "card_service_failed"
        revision, text = await store_.read_of(conn, c.id)
        await store_.failed(conn, c, error, counts=True, text=text)
    return "card_failed"


async def _service_failed(
    conn: psycopg.AsyncConnection, c: Claimed, attempt_id: int, error: str
) -> str:
    """Submitting or following the attempt's job failed: the attempt goes on, and the pass,
    let go still running, is taken by its next claim once the queue's pause is over."""
    log.warning("%s: the deep service failed: %s; the queue waits %s", c.paper.id, error, PAUSE)
    async with conn.transaction():
        await cards.service_failed(conn, attempt_id, error)
        await store_.let_go(conn, c, error)
    return "card_service_failed"


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
