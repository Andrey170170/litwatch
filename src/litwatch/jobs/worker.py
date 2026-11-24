"""The jobs worker (docs/plans/jobs.md): runs the owner's queue in `litwatch serve`, under
`[schedule] enabled`, beside the scheduler.

It works a chunk of papers at a time under the run lock, and starts no chunk while a nightly run
is running or due, so the nightly waits at most one chunk (pipeline.md, section 10). After every
chunk it takes the most urgent runnable job again (`store.jobs.claim`), choosing under the lock,
so a Process job queued meanwhile goes next and no worker passes over an older job. A job is
claimed in the name of the worker's process, its gate's presence session
(`DatabaseGate.identity`); one whose session is gone is put back in the queue, and its steps
then pick only what is still due. Each time it runs a job, from claim
to stop, is one run of kind `job`, however many chunks that takes.

A Process job runs the nightly's own steps (classification, analysis, grading) on its papers
only; each recorded result settles its paper as in the nightly, and the passes it queued are
then worked by their work items, as the owner's requests (`feedback.word.settle_many`).
A job is done once its papers have no step left failed; else it fails, and settles when they
do (`store.jobs.settle_process`).

A re-run (`jobs.reruns`) grades its scope for its topic, then works the text passes it queued,
through a gateway that holds its ceiling: one stopped by it waits on the owner as `stopped`. A
replay (`jobs.replays`) works its papers the same way under its ceiling, writing the evaluation
tables only. A digest written again (`jobs.digests.again`) is one chunk, and goes first, as a
Process job does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core import FAILED_RUNS
from litwatch.feedback import word
from litwatch.gates.database import BusyError, DatabaseGate, Identity
from litwatch.jobs import digests as digest_jobs
from litwatch.jobs import nightly, replays, reruns, scheduler
from litwatch.jobs.context import GatewayFactory, ModelRun, attaching, default_gateway, model_run
from litwatch.quality import replay
from litwatch.store import failures, runs
from litwatch.store import fulltext as fulltext_store
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store.calls import EVAL

log = logging.getLogger(__name__)

CHUNK = 50  # papers a chunk takes: the most the nightly waits behind a job
TICK_SECONDS = 30


async def nightly_waits(db: DatabaseGate, config: Config, now: datetime) -> bool:
    """Whether a nightly run is running, or due at `now` (`scheduler.is_due`) with the schedule
    on, which alone starts it: no chunk, and no pass of a re-run, starts then."""
    nightly = config.schedule.nightly
    async with db.read("does the nightly wait") as unit:
        last = await runs.last_started(unit, "nightly")
        latest = await runs.latest(unit, "nightly")
    if config.schedule.enabled and scheduler.is_due(nightly, now, last):
        return True
    return bool(latest and latest["finished_at"] is None and latest["alive"])


def failure(open_: dict[str, tuple[int, int]]) -> str | None:
    """Why a job whose papers have these failures left (`store.failures.open_on`) failed, or
    None: some paper's step failed, and the paper waits for the next run or, after three runs in
    a row, the owner."""
    failed = [
        f"{step} failed on {n}" + (" (waits on Needs you)" if stuck == n else "")
        for step, (n, stuck) in open_.items()
    ]
    return "; ".join(sorted(failed)) or None


# The most runs a worker holds open: re-runs whose passes await their text stay open while
# others run. Past it only Process jobs start.
OPEN_MOST = 4


@dataclass
class _Open:
    stack: AsyncExitStack
    run: ModelRun
    tried: set[str] = field(default_factory=set)  # a replay's papers this run has tried


class Worker:
    """The queue worked through one gate, in the name of its process's identity
    (`DatabaseGate.identity`), which the jobs it claims run in."""

    def __init__(
        self,
        db: DatabaseGate,
        config: Config,
        gateway: GatewayFactory = default_gateway,
        archive: ArchiveClient | None = None,
    ) -> None:
        self.db, self.config, self.gateway = db, config, gateway
        self.archive = archive  # a re-run's text passes read from it; without, the re-run stops
        self.open: dict[int, _Open] = {}  # the jobs this worker runs, each with its run
        self.me: Identity | None = None  # the identity its open jobs were claimed in

    async def step(self, now: datetime) -> bool:
        """Work one chunk of the most urgent runnable job. False when none could start now: none
        is runnable, a nightly run is due or running, or another job holds the run lock."""
        me = await self.db.identity()
        if me != self.me:
            if self.open:
                # The presence session ended, and with it the claims of the jobs this worker
                # runs: their runs end, and the jobs go back in the queue (`take_back`), from
                # which this worker or another claims them afresh.
                log.warning("jobs: the worker's session ended; its %s jobs go back", len(self.open))
                await self.close(RuntimeError("the worker's session ended"))
            self.me = me
        async with self.db.write("take back jobs") as unit:
            taken = await jobs_store.take_back(unit)
        if taken:
            log.info("jobs: took back %s, whose session is gone", taken)
        if await nightly_waits(self.db, self.config, now):
            return False
        # The choice is made under the lock, so another worker's chunk never runs between
        # choosing a job and working it.
        held = AsyncExitStack()
        try:
            await held.enter_async_context(self.db.held("run"))
        except BusyError:
            return False
        async with held:
            return await self._next()

    @property
    def _me(self) -> Identity:
        assert self.me is not None  # set by `step`, before any job is claimed
        return self.me

    async def _next(self) -> bool:
        async with self.db.write("claim a job") as unit:
            job = await jobs_store.claim(unit, me=self._me, fresh=len(self.open) < OPEN_MOST)
        if job is None:
            return False
        match job["kind"]:
            case "rerun":
                await self._rerun(job)
                return True
            case "replay":
                await self._replay(job)
                return True
            case "digest":
                await self._digest(job)
                return True
        try:
            after = await self._chunk(job, await self._run_of(job))
        except Exception as exc:
            log.exception("jobs: job %s failed", job["id"])
            await self._end(job, f"{type(exc).__name__}: {exc}")
            await self._close(job["id"], exc)
            return True
        if after is None:  # no longer this session's
            await self._close(job["id"])
        elif after["done"] >= after["papers"]:
            await self._end(job)
            await self._close(job["id"])
        return True

    async def _end(self, job: dict[str, Any], error: str | None = None) -> None:
        """End the job: failed with `error`, or failed while a paper has a step left failed (one
        tried again with nothing due, since it waits for the owner, fails again), else done, or
        queued again when the owner acted on its paper while it ran (`store.jobs.finish`); a
        Process job whose paper was dropped meanwhile (dismissed or withdrawn) is discarded.
        Under the papers' settle locks, so an owner's action on them meanwhile settles the job
        after this."""
        papers = job["scope"]["papers"]
        async with self.db.write("end job", taxonomy="shared", papers=papers) as unit:
            if job["kind"] == "process" and await papers_store.dropped(unit, papers[0]):
                state, why = "discarded", None
            else:
                why = error or failure(await failures.open_on(unit, papers, FAILED_RUNS))
                state = "failed" if why else "done"
            await jobs_store.finish(unit, job["id"], state, why, me=self._me)

    async def _finish(self, job: dict[str, Any], state: str, error: str | None = None) -> None:
        async with self.db.write(f"job {state}") as unit:
            await jobs_store.finish(unit, job["id"], state, error, me=self._me)

    async def _chunk(self, job: dict[str, Any], run: ModelRun) -> dict[str, Any] | None:
        """The job's next chunk of papers through classification, analysis and grading; returns
        the job as recorded after it."""
        ids = job["scope"]["papers"][job["done"] : job["done"] + CHUNK]
        before = run.tally()
        await nightly.classify(self.db, self.config, run, ids=ids)
        await nightly.analyse(self.db, self.config, run, ids=ids)
        await nightly.grade(self.db, self.config, run, ids=ids)
        counts = Counter(job["counts"]) + (run.tally() - before)
        done = job["done"] + len(ids)
        async with self.db.write("job progress") as unit:
            return await jobs_store.progress(unit, job["id"], done, dict(counts), me=self._me)

    async def _ceiling(self, job: dict[str, Any], run: ModelRun, roles: tuple[str, ...]) -> None:
        if run.gateway.ceiling is None:
            async with self.db.read("job ceiling") as unit:
                run.gateway.ceiling = await reruns.ceiling(unit, self.config, job, roles)

    async def _rerun(self, job: dict[str, Any]) -> None:
        """A re-run's next chunk. One whose topic is no longer followed and active fails; one
        its ceiling stopped waits on the owner; one that ends lets go of the text passes it
        left (failed, or awaiting text too long) to the regular queue."""
        async with self.db.read("re-run: taxonomy") as unit:
            tree = await taxonomy_store.load(unit)
        topic = reruns.active_topic(tree, job["scope"]["topic"])
        if tree is None or topic is None:
            await self._end_rerun(job, "failed", "the topic is no longer followed and active")
            await self._close(job["id"])
            return
        if reruns.edited_since(job, topic):
            await self._end_rerun(job, "failed", "the topic was edited since; re-run it from there")
            await self._close(job["id"])
            return
        try:
            run = await self._run_of(job)
            await self._ceiling(job, run, reruns.roles(topic))
            chunk = await reruns.work(
                self.db, self.config, run, job, tree, self.archive, CHUNK, me=self._me
            )
        except Exception as exc:
            log.exception("jobs: re-run %s failed", job["id"])
            await self._end_rerun(job, "failed", f"{type(exc).__name__}: {exc}")
            await self._close(job["id"], exc)
            return
        match chunk.outcome:
            case "more":
                return
            case "waiting":  # its passes await their text: other jobs go first meanwhile
                now = datetime.now(self.config.schedule.zone)
                async with self.db.write("re-run waits") as unit:
                    await jobs_store.wait(unit, job["id"], now + reruns.WAKE, me=self._me)
                return
            case "stopped":
                await self._finish(job, "stopped", chunk.where)
            case "done":
                why = f"grading failed on {chunk.failed}" if chunk.failed else None
                await self._end_rerun(job, "failed" if why else "done", why)
        await self._close(job["id"])

    async def _replay(self, job: dict[str, Any]) -> None:
        """A replay's next chunk, under its ceiling; one its ceiling stopped waits on the owner.
        Its run is an evaluation's: it observes nothing of the providers."""
        try:
            run = await self._run_of(job)
            await self._ceiling(job, run, replay.ROLES)
            tried = self.open[job["id"]].tried
            chunk = await replays.work(self.db, self.config, run, job, tried, CHUNK, me=self._me)
        except Exception as exc:
            await self._failed(job, exc)
            return
        match chunk.outcome:
            case "more":
                return
            case "stopped":
                await self._finish(job, "stopped", chunk.where)
            case "done":
                await self._finish(job, "done")
        await self._close(job["id"])

    async def _digest(self, job: dict[str, Any]) -> None:
        """A failed digest written again (`jobs.digests.again`), in one chunk."""
        try:
            run = await self._run_of(job)
            state, why = await digest_jobs.again(
                self.db, run, job["scope"]["digest"], rules=word.rules(self.config)
            )
            async with self.db.write("digest job") as unit:
                await jobs_store.progress(unit, job["id"], 1, dict(run.tally()), me=self._me)
                await jobs_store.finish(unit, job["id"], state, why, me=self._me)
        except Exception as exc:
            await self._failed(job, exc)
            return
        await self._close(job["id"])

    async def _failed(self, job: dict[str, Any], exc: Exception) -> None:
        """End a job that raised as failed, with its error, and close its run with it."""
        log.exception("jobs: %s job %s failed", job["kind"], job["id"])
        await self._finish(job, "failed", f"{type(exc).__name__}: {exc}")
        await self._close(job["id"], exc)

    async def _end_rerun(self, job: dict[str, Any], state: str, error: str | None) -> None:
        """End the re-run and let go of its passes, in one unit, only if it is still this
        worker's: one a peer took back is the peer's, and so are its passes."""
        async with self.db.write("end re-run", taxonomy="shared") as unit:
            if await jobs_store.finish(unit, job["id"], state, error, me=self._me):
                await fulltext_store.release_job(unit, job["id"])

    async def _run_of(self, job: dict[str, Any]) -> ModelRun:
        """The run this worker records the job's work on, of kind `job` (`eval` for a replay):
        opened when it claims the job, kept across its chunks. Its gateway sends no batch, and
        answers a call a dead nightly left in an ended batch from that batch (`attaching`); a
        replay's, as the CLI's, leaves other runs' batches alone."""
        if job["id"] not in self.open:
            stack = AsyncExitStack()
            # A replay's runs are evaluation runs: they observe nothing of the providers.
            replaying = job["kind"] == "replay"
            factory = self.gateway if replaying else attaching(self.gateway)
            kind = EVAL if replaying else "job"
            run = await stack.enter_async_context(
                model_run(self.db, self.config, kind, gateway=factory, job_id=job["id"])
            )
            stack.push_async_callback(run.gateway.close)
            self.open[job["id"]] = _Open(stack, run)
        return self.open[job["id"]].run

    async def _close(self, job_id: int, error: BaseException | None = None) -> None:
        """Close the job's run, with the error that ended it."""
        found = self.open.pop(job_id, None)
        if found is None:
            return
        if error is None:
            await found.stack.aclose()
        else:
            await found.stack.__aexit__(type(error), error, error.__traceback__)

    async def close(self, error: BaseException) -> None:
        """Close every run this worker has open, as ended by `error`; their jobs stay running
        until taken back, once their session is gone."""
        for job_id in list(self.open):
            try:
                await self._close(job_id, error)
            except Exception:
                log.exception("jobs: closing job %s's run failed", job_id)


async def run_forever(
    db: DatabaseGate,
    config: Config,
    gateway: GatewayFactory = default_gateway,
    wake: asyncio.Event | None = None,
    archive: ArchiveClient | None = None,
) -> None:
    """Work the queue until cancelled: chunk after chunk while there is one to run, else look
    every `TICK_SECONDS` or as soon as `wake` is set (a job was queued). A failure (a lost
    database) closes the worker's runs and is retried after a tick; a presence session opened
    again takes back the jobs claimed in the old one's name (`Worker.step`)."""
    wake = wake or asyncio.Event()
    log.info("jobs worker on")
    while True:
        worker = Worker(db, config, gateway, archive)
        try:
            while True:
                if not await worker.step(datetime.now(config.schedule.zone)):
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(wake.wait(), TICK_SECONDS)
                    wake.clear()
        except asyncio.CancelledError as exc:
            await worker.close(exc)
            raise
        except Exception as exc:
            log.exception("jobs worker failed")
            await worker.close(exc)
            await asyncio.sleep(TICK_SECONDS)
