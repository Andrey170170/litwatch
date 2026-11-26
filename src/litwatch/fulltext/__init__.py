"""The full-text queue (docs/specs/v1/pipeline.md, section 6). A settle (`feedback.word`) queues
a paper's Brief or Weight re-check; here passes are worked, by the nightly and a re-run inline,
and otherwise each by its work item (`fulltext.worker`). A pass reads the paper's text from the
archive, asked for at urgent priority. While the archive is still fetching it, the pass is
awaiting text and is tried again after `TEXT_WAIT`, so it runs once the text is there.
With the text, the `brief` role writes the Brief, a Gist and the Weight from the full text, which
`record_in_change` stores and settles. Both kinds of pass make that one call.

Before anything is spent on a claimed pass, its paper is settled (`still_wanted`), so a pass
the database no longer wants is dropped unread, whatever trigger missed it. A pass is held by
a lease while it runs; one whose lease ran out (its worker died) is tried
again. A failed pass is tried again on the next nightly run (`retry_failed`), and the archive
is asked to try again what it gave up on; after `FAILED_RUNS` failed runs, a refusal by every
model of `brief`'s list, or a week awaiting text, it waits for the owner on Quality."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from litwatch import events
from litwatch.archive import (
    URGENT,
    ArchiveClient,
    ArchiveError,
    ArchiveUnavailable,
    PaperText,
    read_text,
    retry_downloads,
)
from litwatch.concurrency import bounded
from litwatch.config import Config
from litwatch.core import Cause
from litwatch.core.fulltext import LEASE, TEXT_WAIT, TEXT_WAVE, PassKind, TextRead
from litwatch.feedback import word
from litwatch.fulltext import brief
from litwatch.gates.database import DatabaseGate, WriteUnit
from litwatch.integrations import IntegrationError
from litwatch.integrations import archive as archive_integration
from litwatch.jobs.context import ModelRun, model_run
from litwatch.llm import CallError, Gateway, ModelGate
from litwatch.llm.batches import DRAIN
from litwatch.store import fulltext as store_
from litwatch.store import outbox
from litwatch.store import papers as papers_store
from litwatch.store.fulltext import Claimed

log = logging.getLogger(__name__)

CONCURRENCY = TEXT_WAVE  # `brief` calls at a time, and archive reads
# Passes claimed at a time: one wave of calls, which ends well within the lease even when each
# call takes its whole timeout and retries. A batching run claims every pass due at once, held
# until its deadline (models.md, batches).
BATCH = CONCURRENCY
TICK_SECONDS = TEXT_WAIT.total_seconds()  # how often `work_waiting` looks again


@dataclass
class Attempt:
    """One try of a pass: the text read and what the `brief` role answered, or why not."""

    claimed: Claimed
    text: PaperText | None = None
    output: brief.Output | None = None
    model: str | None = None
    error: str | None = None
    counts: bool = False  # a failure that counts: no text to be had, or an unusable answer
    unusable: bool = False  # an unusable answer: the next try starts one model further down
    refused: bool = False  # every model of `brief` refused: the pass stops at once
    waiting: bool = False  # no text yet: the archive is fetching it, or cannot be reached
    retry_asked: bool = False  # the archive was asked to retry the downloads it gave up on
    stopped: bool = False  # a re-run's ceiling kept the call from being sent: given back


def archive_for(config: Config) -> ArchiveClient | None:
    """A client for reading texts and serving PDFs: the enabled archive section's, else one from
    $LITWATCH_ARCHIVE_URL and $LITWATCH_ARCHIVE_TOKEN alone (the dev host's way); None when
    neither is set, or when an archive section says `enabled = false`, which wins."""
    if archive_integration.switched_off(config):
        return None
    try:
        return archive_integration.source(config)
    except IntegrationError:
        return None


async def read(archive: ArchiveClient, c: Claimed) -> Attempt:
    """Read the claimed pass's text from the archive at urgent priority: its PDF alone when the
    owner said "Use the PDF"."""
    a = Attempt(c)
    try:
        if c.retry_text:  # tried again: what the archive gave up on is tried again too
            await retry_downloads(archive, c.paper, URGENT, pdf=c.use_pdf)
            a.retry_asked = True
        a.text = await read_text(archive, c.paper, URGENT, pdf=c.use_pdf)
    except ArchiveUnavailable as exc:
        a.waiting, a.error = True, f"the archive: {exc}"
    except ArchiveError as exc:  # TextUnavailable included: no text to be had
        a.error, a.counts = f"no text: {exc}", True
    else:
        a.waiting = a.text is None
    return a


def _write(
    attempts: list[Attempt], gateway: Gateway
) -> contextlib.AbstractAsyncContextManager[AsyncIterator[Attempt]]:
    """Ask the `brief` role about each attempt's text, giving each as it is answered, held with
    `async with` (`concurrency.bounded`: leaving it stops every call)."""

    async def one(a: Attempt) -> Attempt:
        assert a.text is not None
        c = a.claimed
        try:
            answer = await gateway.ask(
                brief.ROLE,
                brief.request(c.paper, c.area, a.text.body, a.text.source),
                paper_id=c.paper.id,
                start=c.unusable_runs,
            )
        except CallError as exc:
            a.error, a.counts, a.refused = str(exc), exc.unusable, exc.kind == "refused"
            a.unusable, a.stopped = exc.unusable, exc.kind == "ceiling"
            return a
        a.output, a.model = answer.value, answer.model
        return a

    return bounded(attempts, one, gateway.width(CONCURRENCY))


def _read_of(text: PaperText | None) -> TextRead | None:
    return TextRead(text.version, text.source, text.sha256) if text else None


async def record_in_change(
    db: DatabaseGate, a: Attempt, rules: word.Rules, counted: store_.Tally | None = None
) -> bool:
    """Store one try's outcome (`record`) as a change of the paper, which settles it: its
    grades follow the Weight, its next pass may be queued (section 8), and a pass back in the
    queue that is no longer wanted (a topic paused while it ran) is dropped. A pass written or
    failed is counted by `counted`, in that unit. Returns False when the worker no longer held
    the pass."""
    c = a.claimed
    async with word.change(
        db, c.paper.id, cause=Cause.VERDICT, rules=rules, job=c.job, name="record text pass"
    ) as done:
        held = await record(done.unit, a)
        if held and counted is not None and _outcome(a) in WORKED:
            await counted(done.unit, 1)
        return held


async def record(unit: WriteUnit, a: Attempt) -> bool:
    """Store one try's outcome, in the unit of a change of the paper. With an answer: the Brief,
    the Weight and the Gist from the full text, labelled with the revision the pass was queued
    for, with `brief.written` and, when that revision is still the paper's, `gist.written`.
    Without: the pass fails or waits for its text, as one for a dropped paper does whatever it
    got. A pass a re-run's ceiling stopped goes back unworked. Returns False when the worker no
    longer held the pass."""
    c = a.claimed
    if not await store_.held(unit, c):
        return False
    read = _read_of(a.text)
    if a.retry_asked:
        await store_.retry_asked(unit, c)
    if a.waiting or await papers_store.dropped(unit, c.paper.id):
        a.waiting = True  # a dropped paper's pass writes nothing; `claim` leaves it alone
        await store_.awaiting(unit, c, a.error)
    elif a.stopped:
        await store_.wait_again(unit, c, a.error or "stopped at the ceiling")
    elif a.output is None or a.model is None or read is None:
        error = a.error or "no answer"
        await store_.failed(
            unit,
            c,
            error,
            counts=a.counts,
            text=read,
            unusable=a.unusable,
            refused=a.refused,
        )
    else:
        await _add(unit, c, a, read)
    return True


async def _add(unit: WriteUnit, c: Claimed, a: Attempt, read: TextRead) -> None:
    """The Brief, the Weight and the Gist from the full text, with their events."""
    assert a.output is not None and a.model is not None
    queued_for = c.paper.model_copy(update={"revision": c.revision})
    current = await papers_store.still_current(unit, queued_for)
    out = a.output
    written = await store_.add(
        unit,
        c,
        read,
        brief=out.brief,
        overstatement=out.overstatement,
        gist=out.gist,
        weight=out.weight,
        weight_reason=out.weight_reason,
        model=a.model,
    )
    await outbox.emit(
        unit,
        events.BRIEF_WRITTEN,
        c.paper.id,
        revision=c.revision,
        arxiv_version=read.version,
        brief_id=written.brief,
    )
    # A Gist from an older revision's full text is never current (section 8).
    if current:
        await outbox.emit(
            unit,
            events.GIST_WRITTEN,
            c.paper.id,
            revision=c.revision,
            source="full_text",
            gist_id=written.gist,
        )


async def still_wanted(db: DatabaseGate, c: Claimed, rules: word.Rules) -> str | None:
    """Settle the paper of a claimed pass before anything is spent on it, under its lock, as
    a settle judges the database now, whatever trigger was missed: None when the pass
    still runs as claimed; else why not. One no longer wanted is dropped; one wanted for
    another reason is relabelled and given back, to run on its next claim; one for a paper
    dropped since its claim is given back, left for the owner's word as `record` leaves it."""
    async with word.change(
        db, c.paper.id, cause=Cause.VERDICT, rules=rules, job=c.job, name="judge claimed pass"
    ) as done:
        return await judged(done, c, rules)


async def judged(done: word.Change, c: Claimed, rules: word.Rules) -> str | None:
    """`still_wanted`'s judgement, in a change of the paper (`word.change`): the Deep worker
    makes it again just before it submits, since the archive read can be long. It settles the
    paper itself, with the pass as claimed, since it reads what that settle decided, and the
    change leaves it settled: what the block writes after it (a pass given back, or submitted)
    is no input a settle reads anew. A pass lost or dismissed is left to the change's settle."""
    unit = done.unit
    if not await store_.held(unit, c):
        return "lost"
    if await papers_store.dropped(unit, c.paper.id):  # a settle leaves its pass alone
        await store_.release(unit, c)
        return "fulltext_dismissed"
    done.settled |= await word.settle_many(
        unit, [c.paper.id], cause=Cause.VERDICT, rules=rules, claimed=c.id, job=c.job
    )
    label = await store_.label_of(unit, c.id)
    if label is None:
        return "fulltext_dropped"
    # A Deep pass the owner asked for meanwhile runs all the same, now as their request.
    if label != (c.kind, c.reason) and c.kind != PassKind.DEEP:
        await store_.release(unit, c)
        return "fulltext_relabelled"
    return None


def _outcome(a: Attempt) -> str:
    if a.waiting:
        return "awaiting_text"
    if a.stopped:
        return "fulltext_stopped"
    return "briefs_written" if a.output is not None else "fulltext_failed"


# The outcomes of the passes a try spent on: written or failed, not those awaiting text.
WORKED = ("briefs_written", "fulltext_failed")


def worked(counts: Counter[str]) -> int:
    """How many passes `work`'s tries spent on (`WORKED`)."""
    return sum(counts[k] for k in WORKED)


async def work(
    db: DatabaseGate,
    config: Config,
    archive: ArchiveClient,
    *,
    run: ModelRun | None = None,
    models: ModelGate | None = None,
    limit: int | None = None,
    deadline: float | None = None,
    job: int | None = None,
    counted: store_.Tally | None = None,
) -> Counter[str]:
    """Try every pass that is due once, up to `limit`, `BATCH` at a time: read its text, and write
    from it, each pass worked counted by `counted` as it is recorded. No batch starts after
    `deadline` (`time.monotonic()`); those left are the worker's. The calls go on `run`, or on a
    run of kind `fulltext` opened through `models` only when some text arrived. Returns how the
    tries ended.

    A `run` (a nightly) leaves the owner's passes to their work items; batching, it claims all
    the automatic ones due at once, held until its batches' deadline. Without a run, while
    another job holds the run lock, only the owner's are worked (models.md, batches). A re-run's
    passes are worked only by its `job`, on its run, until its ceiling is reached."""
    counts: Counter[str] = Counter()
    tried: set[int] = set()
    lease, wide, owners = LEASE, False, False if run and job is None else None
    if run and (until := run.gateway.batching_until()):
        lease, wide = until - datetime.now(UTC) + DRAIN + LEASE, True
    while (
        (limit is None or len(tried) < limit)
        and (deadline is None or time.monotonic() < deadline)
        and not (run and run.gateway.ceiling and run.gateway.ceiling.reached)
    ):
        if run is None:  # asked at each claim: a nightly may have started meanwhile
            owners = True if await db.held_elsewhere("run") else None
        size = BATCH if limit is None else min(BATCH, limit - len(tried))
        if wide:
            size = None if limit is None else limit - len(tried)
        async with db.write("claim text passes") as unit:
            found = await store_.claim(unit, lease, size, tried, owners=owners, job=job)
        claimed = [c for c in found if c.id not in tried]
        if not claimed:
            break
        tried.update(c.id for c in claimed)
        counts += await wave(db, config, archive, claimed, run=run, models=models, counted=counted)
    return counts


async def wave(
    db: DatabaseGate,
    config: Config,
    archive: ArchiveClient,
    claimed: list[Claimed],
    *,
    run: ModelRun | None = None,
    models: ModelGate | None = None,
    counted: store_.Tally | None = None,
) -> Counter[str]:
    """Try the claimed passes once: settle each paper (`still_wanted`), read the texts, and
    write from them, `CONCURRENCY` at a time. The calls go on `run`, or on a run of kind
    `fulltext` opened through `models` only when some text arrived. Returns how the tries
    ended."""
    counts: Counter[str] = Counter()
    rules = word.rules(config)
    reads = asyncio.Semaphore(CONCURRENCY)

    async def read_one(c: Claimed) -> Attempt:
        async with reads:
            return await read(archive, c)

    wanted = []
    for c in claimed:
        if (why := await still_wanted(db, c, rules)) is None:
            wanted.append(c)
        elif why != "lost":
            counts[why] += 1
            if run:
                run.add(why)
    attempts = await asyncio.gather(*(read_one(c) for c in wanted))
    for a in attempts:
        if a.text is None and await record_in_change(db, a, rules, counted):
            counts[_outcome(a)] += 1
            if run:
                run.add(_outcome(a))
    texts = [a for a in attempts if a.text is not None]
    if not texts:
        return counts
    if run is not None:
        opened = contextlib.nullcontext(run)
    else:
        assert models is not None, "a wave's calls go on a run, or on one it opens"
        opened = model_run(db, "fulltext", models=models)
    async with opened as r, _write(texts, r.gateway) as answered:
        async for a in answered:
            if await record_in_change(db, a, rules, counted):
                counts[_outcome(a)] += 1
                r.add(_outcome(a))
    return counts


async def work_waiting(
    db: DatabaseGate,
    config: Config,
    archive: ArchiveClient,
    *,
    limit: int | None = None,
    wait: float = 0,
    models: ModelGate,
    report: Callable[[Counter[str]], Awaitable[None]] | None = None,
    counted: store_.Tally | None = None,
) -> Counter[str]:
    """`work`, and again every `TICK_SECONDS` for at most `wait` seconds while some passes await
    their text (`litwatch fulltext --wait`), reporting each round. `limit` caps the passes
    worked, written or failed, over all the rounds; `counted` counts each (`work`). Returns how
    the tries ended."""
    total: Counter[str] = Counter()
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        allowance = None if limit is None else limit - worked(total)
        if allowance is not None and allowance <= 0:
            return total
        done = await work(db, config, archive, models=models, limit=allowance, counted=counted)
        total.update(done)
        if report:
            await report(done)
        left = deadline - asyncio.get_running_loop().time()
        if not done["awaiting_text"] or left <= 0:
            return total
        await asyncio.sleep(min(TICK_SECONDS, left))
