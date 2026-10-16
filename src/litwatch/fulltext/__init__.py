"""The full-text queue (docs/specs/v1/pipeline.md, section 6). `feedback.word.settle` queues a
paper's Brief or Weight re-check; here passes are worked. A pass reads the paper's text from the
archive, asked for at urgent priority. While the archive is still fetching it, the pass is
awaiting text and is tried again on the worker's next tick, so it runs once the text is there.
With the text, the `brief` role writes the Brief, a Gist and the Weight from the full text, which
`record` stores and settles. Both kinds of pass make that one call.

Before anything is spent on a claimed pass, its paper is settled (`still_wanted`), so a pass
the database no longer wants is dropped unread, whatever trigger missed it. A pass is held by
a lease while it runs; one whose lease ran out (its worker died) is tried
again. A failed pass is tried again on the next nightly run (`retry_failed`), and the archive
is asked to try again what it gave up on; after `FAILED_RUNS` failed runs, or a week awaiting
text, it waits for the owner on Quality."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from litwatch import events, store
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
from litwatch.core.fulltext import LEASE, PassKind, TextRead
from litwatch.feedback import word
from litwatch.fulltext import brief
from litwatch.integrations import IntegrationError
from litwatch.integrations import archive as archive_integration
from litwatch.jobs.context import GatewayFactory, ModelRun, default_gateway, model_run
from litwatch.jobs.lock import held_elsewhere
from litwatch.llm import CallError, Gateway
from litwatch.llm.batches import DRAIN
from litwatch.store import fulltext as store_
from litwatch.store import papers as papers_store
from litwatch.store.calls import CallJournal
from litwatch.store.fulltext import Claimed

log = logging.getLogger(__name__)

CONCURRENCY = 4  # `brief` calls at a time, and archive reads
# Passes claimed at a time: one wave of calls, which ends well within the lease even when each
# call takes its whole timeout and retries. A batching run claims every pass due at once, held
# until its deadline (models.md, batches).
BATCH = CONCURRENCY
TICK_SECONDS = 30  # how often the service's worker looks at the queue


@dataclass
class Attempt:
    """One try of a pass: the text read and what the `brief` role answered, or why not."""

    claimed: Claimed
    text: PaperText | None = None
    output: brief.Output | None = None
    model: str | None = None
    error: str | None = None
    counts: bool = False  # a failure that counts: no text to be had, or an unusable answer
    waiting: bool = False  # no text yet: the archive is fetching it, or cannot be reached
    retry_asked: bool = False  # the archive was asked to retry the downloads it gave up on


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
            )
        except CallError as exc:
            a.error, a.counts = str(exc), exc.unusable
            return a
        a.output, a.model = answer.value, answer.model
        return a

    return bounded(attempts, one, gateway.width(CONCURRENCY))


def _attaching(factory: GatewayFactory) -> GatewayFactory:
    """`factory`'s gateways, sending no batch but waiting on a call a dead run left in an open
    batch rather than paying for it again. The batch is past that run's deadline: it is
    cancelled at once, and its answers kept."""

    def gateway(config: Config, journal: CallJournal) -> Gateway:
        found = factory(config, journal)
        found.batch_until(datetime.now(UTC))
        return found

    return gateway


def _read_of(text: PaperText | None) -> TextRead | None:
    return TextRead(text.version, text.source, text.sha256) if text else None


async def record(conn: psycopg.AsyncConnection, a: Attempt) -> bool:
    """Store one try's outcome. With an answer: the Brief, the Weight and the Gist from the full
    text, labelled with the revision the pass was queued for, with `brief.written` and, when
    that revision is still the paper's, `gist.written`. Without: the pass fails or waits for its
    text, as one for a dismissed paper does whatever it got. Then settle the paper: its grades
    follow the Weight, its next pass may be queued (section 8), and a pass back in the queue
    that is no longer wanted (a topic paused while it ran) is dropped. Returns False when the
    worker no longer held the pass."""
    c = a.claimed
    async with word.change(conn, c.paper.id):
        if not await store_.held(conn, c):
            return False
        read = _read_of(a.text)
        if a.retry_asked:
            await store_.retry_asked(conn, c)
        if a.waiting or await store_.dismissed(conn, c.paper.id):
            a.waiting = True  # a dismissed paper's pass writes nothing; `claim` leaves it alone
            await store_.awaiting(conn, c, a.error)
        elif a.output is None or a.model is None or read is None:
            await store_.failed(conn, c, a.error or "no answer", counts=a.counts, text=read)
        else:
            await _add(conn, c, a, read)
    return True


async def _add(conn: psycopg.AsyncConnection, c: Claimed, a: Attempt, read: TextRead) -> None:
    """The Brief, the Weight and the Gist from the full text, with their events."""
    assert a.output is not None and a.model is not None
    queued_for = c.paper.model_copy(update={"revision": c.revision})
    current = await papers_store.still_current(conn, queued_for)
    out = a.output
    written = await store_.add(
        conn,
        c,
        read,
        brief=out.brief,
        overstatement=out.overstatement,
        gist=out.gist,
        weight=out.weight,
        weight_reason=out.weight_reason,
        model=a.model,
    )
    await events.emit(
        conn,
        events.BRIEF_WRITTEN,
        c.paper.id,
        revision=c.revision,
        arxiv_version=read.version,
        brief_id=written.brief,
    )
    # A Gist from an older revision's full text is never current (section 8).
    if current:
        await events.emit(
            conn,
            events.GIST_WRITTEN,
            c.paper.id,
            revision=c.revision,
            source="full_text",
            gist_id=written.gist,
        )


async def still_wanted(conn: psycopg.AsyncConnection, c: Claimed) -> str | None:
    """Settle the paper of a claimed pass before anything is spent on it, under its lock, as
    `word.settle` judges the database now, whatever trigger was missed: None when the pass
    still runs as claimed; else why not. One no longer wanted is dropped; one wanted for
    another reason is relabelled and given back, to run on its next claim; one for a paper
    dismissed since its claim is given back, left for the owner's word as `record` leaves it."""
    async with word.change(conn, c.paper.id):
        return await judged(conn, c)


async def judged(conn: psycopg.AsyncConnection, c: Claimed) -> str | None:
    """`still_wanted`'s judgement, inside a change of the paper (`word.change`): the Deep worker
    makes it again just before it submits, since the archive read can be long. It settles the
    paper itself, with the pass as claimed, since it reads what that settle decided; the
    change's own settle then finds the pass running, or given back as judged, and leaves it."""
    if not await store_.held(conn, c):
        return "lost"
    if await store_.dismissed(conn, c.paper.id):  # `settle` leaves its pass alone
        await store_.release(conn, c)
        return "fulltext_dismissed"
    await word.settle(conn, c.paper.id, claimed=c.id)
    label = await store_.label_of(conn, c.id)
    if label is None:
        return "fulltext_dropped"
    # A Deep pass the owner asked for meanwhile runs all the same, now as their request.
    if label != (c.kind, c.reason) and c.kind != PassKind.DEEP:
        await store_.release(conn, c)
        return "fulltext_relabelled"
    return None


def _outcome(a: Attempt) -> str:
    if a.waiting:
        return "awaiting_text"
    return "briefs_written" if a.output is not None else "fulltext_failed"


def worked(counts: Counter[str]) -> int:
    """How many passes `work`'s tries spent on: written or failed, not those awaiting text."""
    return counts["briefs_written"] + counts["fulltext_failed"]


async def work(
    conn: psycopg.AsyncConnection,
    config: Config,
    archive: ArchiveClient,
    *,
    run: ModelRun | None = None,
    gateway: GatewayFactory = default_gateway,
    limit: int | None = None,
    deadline: float | None = None,
) -> Counter[str]:
    """Try every pass that is due once, up to `limit`, `BATCH` at a time: read its text, and
    write from it. No batch starts after `deadline` (`time.monotonic()`); those left are the
    worker's. The calls go on `run`, or on a run of kind `fulltext` opened only when some text
    arrived. Returns how the tries ended.

    A batching `run` claims all the automatic passes due at once, held until its gateway's
    deadline, and leaves the owner's to the service's worker; without a run, while another
    job holds the run lock, only the owner's are worked (models.md, batches)."""
    counts: Counter[str] = Counter()
    tried: set[int] = set()
    lease, wide, owners = LEASE, False, None
    if run and (until := run.gateway.batching_until()):
        lease, wide, owners = until - datetime.now(UTC) + DRAIN + LEASE, True, False
    reads = asyncio.Semaphore(CONCURRENCY)

    async def read_one(c: Claimed) -> Attempt:
        async with reads:
            return await read(archive, c)

    while (limit is None or len(tried) < limit) and (
        deadline is None or time.monotonic() < deadline
    ):
        if run is None:  # asked at each claim: a nightly may have started meanwhile
            owners = True if await held_elsewhere(conn) else None
        size = BATCH if limit is None else min(BATCH, limit - len(tried))
        if wide:
            size = None if limit is None else limit - len(tried)
        claimed = [
            c
            for c in await store_.claim(conn, lease, size, tried, owners=owners)
            if c.id not in tried
        ]
        if not claimed:
            break
        tried.update(c.id for c in claimed)
        wanted = []
        for c in claimed:
            if (why := await still_wanted(conn, c)) is None:
                wanted.append(c)
            elif why != "lost":
                counts[why] += 1
                if run:
                    run.add(why)
        attempts = await asyncio.gather(*(read_one(c) for c in wanted))
        for a in attempts:
            if a.text is None and await record(conn, a):
                counts[_outcome(a)] += 1
                if run:
                    run.add(_outcome(a))
        texts = [a for a in attempts if a.text is not None]
        if not texts:
            continue
        # With no nightly running, a key in an open batch is a dead run's (models.md, batches).
        factory = _attaching(gateway) if owners is None else gateway
        opened = (
            contextlib.nullcontext(run)
            if run
            else model_run(conn, config, "fulltext", gateway=factory)
        )
        async with opened as r, _write(texts, r.gateway) as answered:
            async for a in answered:
                if await record(conn, a):
                    counts[_outcome(a)] += 1
                    r.add(_outcome(a))
    return counts


async def work_waiting(
    conn: psycopg.AsyncConnection,
    config: Config,
    archive: ArchiveClient,
    *,
    limit: int | None = None,
    wait: float = 0,
    gateway: GatewayFactory = default_gateway,
    report: Callable[[Counter[str]], None] | None = None,
) -> Counter[str]:
    """`work`, and again every `TICK_SECONDS` for at most `wait` seconds while some passes await
    their text (`litwatch fulltext --wait`), reporting each round. `limit` caps the passes
    worked, written or failed, over all the rounds. Returns how the tries ended."""
    total: Counter[str] = Counter()
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        allowance = None if limit is None else limit - worked(total)
        if allowance is not None and allowance <= 0:
            return total
        done = await work(conn, config, archive, gateway=gateway, limit=allowance)
        total.update(done)
        if report:
            report(done)
        left = deadline - asyncio.get_running_loop().time()
        if not done["awaiting_text"] or left <= 0:
            return total
        await asyncio.sleep(min(TICK_SECONDS, left))


async def run_forever(
    dsn: str,
    config: Config,
    archive: ArchiveClient,
    gateway: GatewayFactory = default_gateway,
    wake: asyncio.Event | None = None,
) -> None:
    """The service's worker: work the queue every `TICK_SECONDS`, or as soon as `wake` is set
    (the owner ran a pass now), until cancelled. Each tick opens its own connection, so a lost
    database is retried rather than fatal."""
    wake = wake or asyncio.Event()
    log.info("full-text worker on")
    while True:
        try:
            async with await store.connect(dsn) as conn:
                if done := await work(conn, config, archive, gateway=gateway):
                    log.info("full-text queue: %s", dict(done))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("full-text queue tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), TICK_SECONDS)
        wake.clear()
