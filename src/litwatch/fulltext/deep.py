"""Deep card passes (docs/specs/v1/pipeline.md, section 6; integrations.md). A settle queues
an automatic one for a Focus must read or a published Area pick with no card, unless it is
unsaved, and `request` the owner's, which no settle drops. Each is worked by its work item
(`fulltext.worker`), one at a time, since a card takes up to most of an hour: a claimed pass is
settled under its paper's lock before anything is spent (`still_wanted`), its text is read from
the archive at urgent priority, and the `deep` integration's service is given it.

Each submission is an attempt, recorded before it is sent and given its job once accepted, so a
worker that dies while a job runs leaves the job to the next claim to follow, never paid for
twice. A submission carries its attempt as a key, by which the service finds the job it
started for it: one whose answer never came is followed if the service has its job, and
otherwise ends, its pass tried again as a first try is. When a
job ends, with a card or failed, the attempt records the job's usage, or that it is unknown
(the job was lost before it reported), whose failure it was, its trace and its review rounds.
A card is stored with its figures, which replace the paper's earlier ones, in one transaction
with the pass's end and `card.ready`.

A job that failed because of the paper (no card within its budget, an invalid card) fails the
pass: it is tried again on the next nightly run, and after `FAILED_RUNS` failed runs it waits
for the owner on Quality, as every pass does. One that failed because of the service (the
service tagged it so, lost it, never ended it, or returned a result litwatch cannot store: an
unknown format, figures that are not what the contract says) counts against nothing: the pass
goes back in the queue. Nor does an error submitting or following a job, after which the
attempt and the pass stay as they are: the job is followed, found by its key if its
submission's answer was lost, or, when the service has none, the attempt ends and the pass is
tried afresh. A job not ended by its deadline is kept too and followed again after each pause
until it ends; one running past the deadline by the service's own clock, which is shorter, is
given up on as the service's failure. Any service failure pauses the whole queue for `PAUSE`.
There is no daily cap; the owner's
requests go first.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx

from litwatch import events
from litwatch.archive import ArchiveClient
from litwatch.core import Cause
from litwatch.core.fulltext import REQUESTED, TextRead
from litwatch.feedback import word
from litwatch.fulltext import judged, read, record_in_change, still_wanted
from litwatch.gates.database import DatabaseGate, WriteUnit
from litwatch.integrations.deep import Deep, RejectedError
from litwatch.store import fulltext as store_
from litwatch.store import integrations as cards
from litwatch.store import jobs as jobs_store
from litwatch.store import outbox
from litwatch.store.fulltext import Claimed

log = logging.getLogger(__name__)

# After a service failure the queue waits this long before its next try.
PAUSE = timedelta(minutes=15)
# The usage of a submission the service turned down: nothing ran, so it is known, and zero.
NOTHING_RAN = dict.fromkeys(
    ("turns", "cells", "seconds", "input", "output", "reasoning", "cache_read", "list_cost"), 0
)


async def request(unit: WriteUnit, paper_id: str) -> int | None:
    """The owner asks for a Deep card on the paper, whatever its grade, a card already made
    included ("Write again"): their request takes over the open Deep pass or queues one, with
    `card.requested`, in a unit holding the paper's lock, which settles the paper after it (a
    change of the paper, `word.change`). Returns the pass's id; None for a paper that is not
    there or dropped, whose passes are never worked."""
    found = await store_.deep_facts(unit, paper_id)
    if found is None or found.dropped:
        return None
    pass_id = await store_.request_card(unit, paper_id, found.revision)
    await outbox.emit(unit, events.CARD_REQUESTED, paper_id, pass_id=pass_id)
    return pass_id


async def work(
    db: DatabaseGate,
    archive: ArchiveClient,
    deep: Deep,
    *,
    rules: word.Rules,
    limit: int | None = None,
    fires: bool = True,
    left: Callable[[str], Awaitable[None]] | None = None,
) -> Counter[str]:
    """Work the Deep passes that are due, one at a time, up to `limit` claimed (`litwatch
    fulltext --deep`); their papers are settled by `rules`. A re-run's (background) passes
    wait while a nightly run is running or due (pipeline.md, section 10): a scheduled fire
    counts with `fires`, the configured schedule on (`store.jobs.nightly_waits`). Returns how
    the tries ended. Stopped (the command's item ended under it), it lets go of the pass it
    works (`_let_go`), telling `left` of one whose job goes on."""
    counts: Counter[str] = Counter()
    tried: set[int] = set()
    while limit is None or len(tried) < limit:
        if await paused(db):
            break
        async with db.write("claim a deep pass") as unit:
            # Asked at each claim: a nightly may have come due meanwhile.
            background = not await jobs_store.nightly_waits(unit, fires=fires)
            claimed = await store_.claim(
                unit, deep.lease, 1, tried, deep=True, background=background
            )
            # A pass with an attempt open has started: its job is followed whatever the paper
            # wants.
            attempt = await cards.open_attempt(unit, claimed[0].id) if claimed else None
        if not claimed:
            break
        (c,) = claimed
        tried.add(c.id)
        try:
            why = await work_one(db, archive, deep, c, attempt, rules)
        except BaseException:
            await _let_go(db, c, left)
            raise
        if why != "lost":
            counts[why] += 1
    return counts


async def _let_go(
    db: DatabaseGate, c: Claimed, left: Callable[[str], Awaitable[None]] | None
) -> None:
    """A pass cut off as its command stopped: one with no job submitted is given back unworked
    (`store.fulltext.release`), for the next claim at once. One with an attempt open has a job
    that goes on: its lease ends now (`store.fulltext.let_go`), so its next claim, its own work
    item's or the command's next item's, follows the job by its key, never paying for it
    twice; `left` is told of it. A failure is logged: the pass then waits out its lease."""
    try:
        async with db.write("deep pass: let go") as unit:
            attempt = await cards.open_attempt(unit, c.id)
            if attempt is None:
                await store_.release(unit, c)
            else:
                stopped = "its command stopped while its deep job ran"
                await store_.let_go(unit, c, stopped, datetime.now(UTC))
        if attempt is not None and left is not None:
            await left(c.paper.id)
    except Exception:
        log.warning("%s: could not let go of its deep pass", c.paper.id, exc_info=True)


async def paused(db: DatabaseGate) -> datetime | None:
    """Until when the Deep queue waits after a service failure, if it does."""
    async with db.read("deep queue: paused") as unit:
        until = await cards.paused_until(unit, PAUSE)
    if until:
        log.debug("the deep queue waits after a service failure, until %s", until)
    return until


async def work_one(
    db: DatabaseGate,
    archive: ArchiveClient,
    deep: Deep,
    c: Claimed,
    attempt: dict | None,
    rules: word.Rules,
) -> str:
    """Work one claimed Deep pass, `attempt` its open attempt if it has one: settled first
    unless its job was submitted (`still_wanted`), then worked (`_work_one`). Returns how the
    try ended."""
    why = None if attempt else await still_wanted(db, c, rules)
    return why or await _work_one(db, archive, deep, c, attempt, rules)


async def _work_one(
    db: DatabaseGate,
    archive: ArchiveClient,
    deep: Deep,
    c: Claimed,
    attempt: dict | None,
    rules: word.Rules,
) -> str:
    """Follow the pass's job, one left running by a failed poll or a dead worker, or one a
    submission whose answer never came started, found by its key; else read the paper's text
    and submit it. Then record how the job ended. What it writes about the paper goes in
    changes of it; the attempt's own bookkeeping in units of its own."""
    if attempt is not None and attempt["job_id"] is None:
        try:
            job_id = await deep.client.job_for(_key(attempt["id"], c))
        except httpx.HTTPError as exc:
            return await _service_failed(db, c, attempt["id"], f"finding the deep job: {exc}")
        if job_id is not None:
            await _job_started(db, attempt["id"], job_id)
            attempt = {**attempt, "job_id": job_id}  # followed from when it was submitted
        else:
            # Its submission started no job the service has: nothing runs, so the attempt ends
            # (its usage unknown, should a restarted service have lost one) and the pass is
            # tried as a first try is.
            async with db.write("deep attempt: no job") as unit:
                await cards.attempt_ended(
                    unit, attempt["id"], None, "its submission started no job"
                )
            attempt = None
            if why := await still_wanted(db, c, rules):
                return why
    if attempt is None:
        a = await read(archive, c)
        if a.text is None:  # awaiting text, or none to be had
            await record_in_change(db, a, rules)
            return "awaiting_text" if a.waiting else "card_failed"
        text = TextRead(a.text.version, a.text.source, a.text.sha256)
        async with word.change(
            db, c.paper.id, cause=Cause.VERDICT, rules=rules, name="submit deep pass"
        ) as done:
            # Under the paper's lock, judged again as at its claim: the paper may have been
            # dropped or unsaved during the read, and nothing is spent then. The owner's
            # request may have taken the pass over since its claim, and then the submission and
            # its attempt are theirs.
            unit = done.unit
            if why := await judged(done, c, rules):
                return why
            reason = await store_.submitted(unit, c, text)
            if reason is None:
                return "lost"
            requested = reason == REQUESTED
            submission = await deep.submission(unit, c.paper, a.text)
            attempt_id = await cards.add_attempt(unit, c.paper.id, c.id, automatic=not requested)
        try:
            job_id = await deep.client.submit(submission, key=_key(attempt_id, c))
        except RejectedError as exc:
            # Sent again it would be turned down again: the paper's failure, which counts. A
            # wrong extension name in the configuration shows so too, as its topic's papers stop.
            error = f"the deep service rejected the submission: {exc}"
            return await _ended(
                db, c, attempt_id, NOTHING_RAN, error, "paper", rules=rules, ran=False
            )
        except httpx.HTTPError as exc:
            # The service may have taken it all the same: found by its key after the pause.
            return await _service_failed(db, c, attempt_id, f"submitting failed: {exc}")
        await _job_started(db, attempt_id, job_id)
        attempt = {"id": attempt_id, "job_id": job_id, "at": datetime.now(UTC)}
    try:
        outcome = await deep.client.follow(attempt["job_id"], attempt["at"], label=c.paper.id)
    except httpx.HTTPError as exc:
        # The job goes on: followed again by the pass's next claim, after the pause.
        return await _service_failed(db, c, attempt["id"], f"following the deep job: {exc}")
    if outcome.still_running:
        # Past its deadline the job may still run: kept, and followed again after the pause,
        # never submitted again while it may.
        return await _service_failed(db, c, attempt["id"], outcome.error or "")
    if outcome.error or outcome.result is None:
        return await _ended(
            db,
            c,
            attempt["id"],
            outcome.usage,
            outcome.error or "no card",
            "service" if outcome.failure == "service" else "paper",
            outcome.trace,
            outcome.reviews,
            rules=rules,
        )
    async with word.change(
        db, c.paper.id, cause=Cause.VERDICT, rules=rules, name="record deep card"
    ) as done:
        unit = done.unit
        if not await store_.held(unit, c):
            return "lost"
        revision, text = await store_.read_of(unit, c.id)
        assert text is not None  # recorded before the job was submitted
        await cards.attempt_ended(
            unit, attempt["id"], outcome.usage, None, trace=outcome.trace, reviews=outcome.reviews
        )
        await cards.put_card(
            unit,
            c.paper.id,
            outcome.result,
            outcome.model,
            outcome.usage or {},
            revision=revision,
            version=text.version,
            source=text.source,
            pass_id=c.id,
            figures=outcome.figures,
        )
        await store_.done(unit, c, text)
        await outbox.emit(unit, events.CARD_READY, c.paper.id, model=outcome.model, pass_id=c.id)
    usage = outcome.usage or {}
    log.info(
        "%s: deep card in %s turns, list cost $%.2f",
        c.paper.id,
        usage.get("turns"),
        usage.get("list_cost", 0.0),
    )
    return "cards_written"


async def _job_started(db: DatabaseGate, attempt_id: int, job_id: str) -> None:
    async with db.write("deep attempt: job started") as unit:
        await cards.job_started(unit, attempt_id, job_id)


def _key(attempt_id: int, c: Claimed) -> str:
    """A submission's idempotency key: its attempt, and the paper, which no other database's
    attempt of the same id is likely to share."""
    return f"{attempt_id}:{c.paper.id}"


async def _ended(
    db: DatabaseGate,
    c: Claimed,
    attempt_id: int,
    usage: dict | None,
    error: str,
    failure: Literal["paper", "service"],
    trace: list | None = None,
    reviews: list | None = None,
    *,
    rules: word.Rules,
    ran: bool = True,
) -> str:
    """An attempt that made no card: its usage (None, unknown), the error, whose `failure` it
    was, its trace (None, there is none) and its review rounds (None, none ran). The paper's
    fails the pass for this run, a run that counts; the service's puts it back in the queue,
    which pauses. A job that never `ran` leaves the card's last stats as they were."""
    log.warning("%s: deep card failed (%s): %s", c.paper.id, failure, error)
    async with word.change(
        db, c.paper.id, cause=Cause.VERDICT, rules=rules, name="record deep failure"
    ) as done:
        unit = done.unit
        await cards.attempt_ended(unit, attempt_id, usage, error, failure, trace, reviews)
        if not await store_.held(unit, c):
            return "lost"
        await cards.card_failed(unit, c.paper.id, error, usage if ran else None)
        if failure == "service":
            await store_.wait_again(unit, c, error, await _pause_end(unit))
            return "card_service_failed"
        revision, text = await store_.read_of(unit, c.id)
        await store_.failed(unit, c, error, counts=True, text=text)
    return "card_failed"


async def _service_failed(db: DatabaseGate, c: Claimed, attempt_id: int, error: str) -> str:
    """Submitting or following the attempt's job failed: the attempt goes on, and the pass,
    let go still running, is taken by its next claim once the queue's pause is over."""
    log.warning("%s: the deep service failed: %s; the queue waits %s", c.paper.id, error, PAUSE)
    async with db.write("deep service failed") as unit:
        await cards.service_failed(unit, attempt_id, error)
        await store_.let_go(unit, c, error, await _pause_end(unit))
    return "card_service_failed"


async def _pause_end(unit: WriteUnit) -> datetime:
    """When the pause a service failure just recorded in `unit` ends, as every Deep pass
    reads it (`paused`): the one deadline the pass that failed and the others wait for (now,
    when there is no pause to wait out)."""
    return await cards.paused_until(unit, PAUSE) or datetime.now(UTC)
