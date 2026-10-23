"""The jobs worker (docs/plans/jobs.md): runs the owner's queue in `litwatch serve`, under
`[schedule] enabled`, beside the scheduler.

It works a chunk of papers at a time under the run lock, and starts no chunk while a nightly run
is running or due, so the nightly waits at most one chunk (pipeline.md, section 10). After every
chunk it takes the most urgent runnable job again (`store.jobs.claim`), choosing under the lock,
so a Process job queued meanwhile goes next and no worker passes over an older job. A job is
claimed in the name of the worker's database session; one whose session is gone is put back in
the queue, and its steps then pick only what is still due. Each time it runs a job, from claim
to stop, is one run of kind `job`, however many chunks that takes.

A Process job runs the nightly's own steps (classification, analysis, grading) on its papers
only; each recorded result settles its paper as in the nightly, and the full-text and Deep
workers then take the passes it queued, as the owner's requests (`feedback.word.settle`). A job
is done once its papers have no step left failed; else it fails, and settles when they do
(`store.jobs.settle_process`).

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
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg

from litwatch import store
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core import FAILED_RUNS
from litwatch.feedback import word
from litwatch.jobs import digests as digest_jobs
from litwatch.jobs import nightly, replays, reruns, scheduler
from litwatch.jobs.context import GatewayFactory, ModelRun, attaching, default_gateway, model_run
from litwatch.jobs.lock import BusyError, run_lock
from litwatch.store import failures, runs
from litwatch.store import fulltext as fulltext_store
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store

log = logging.getLogger(__name__)

CHUNK = 50  # papers a chunk takes: the most the nightly waits behind a job
TICK_SECONDS = 30


async def nightly_waits(conn: psycopg.AsyncConnection, config: Config, now: datetime) -> bool:
    """Whether a nightly run is running, or due at `now` (`scheduler.is_due`) with the schedule
    on, which alone starts it: no chunk, and no pass of a re-run, starts then."""
    nightly = config.schedule.nightly
    if config.schedule.enabled and scheduler.is_due(
        nightly, now, await runs.last_started(conn, "nightly")
    ):
        return True
    latest = await runs.latest(conn, "nightly")
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


# The most runs a worker holds open, each with a connection of its own: re-runs whose passes
# await their text stay open while others run. Past it only Process jobs start.
OPEN_MOST = 4


@dataclass
class _Open:
    stack: AsyncExitStack
    run: ModelRun
    tried: set[str] = field(default_factory=set)  # a replay's papers this run has tried


class Worker:
    """The queue worked on one connection, whose session the jobs it claims run in."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection,
        config: Config,
        gateway: GatewayFactory = default_gateway,
        worked: Callable[[], None] = lambda: None,
        archive: ArchiveClient | None = None,
    ) -> None:
        self.conn, self.config, self.gateway = conn, config, gateway
        self.worked = worked  # after each chunk: have the full-text and Deep workers look
        self.archive = archive  # a re-run's text passes read from it; without, the re-run stops
        self.open: dict[int, _Open] = {}  # the jobs this worker runs, each with its run

    async def step(self, now: datetime) -> bool:
        """Work one chunk of the most urgent runnable job. False when none could start now: none
        is runnable, a nightly run is due or running, or another job holds the run lock."""
        taken = await jobs_store.take_back(self.conn)
        if taken:
            log.info("jobs: took back %s, whose session is gone", taken)
        if await nightly_waits(self.conn, self.config, now):
            return False
        try:
            # The choice is made under the lock, so another worker's chunk never runs between
            # choosing a job and working it.
            async with run_lock(self.conn):
                return await self._next()
        except BusyError:
            return False

    async def _next(self) -> bool:
        job = await jobs_store.claim(self.conn, fresh=len(self.open) < OPEN_MOST)
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
        self.worked()
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
        Process job whose paper the owner dismissed meanwhile is discarded. Under the papers'
        settle locks, so an owner's action on them meanwhile settles the job after this."""
        papers = job["scope"]["papers"]
        async with self.conn.transaction():
            await word.lock_many(self.conn, papers)
            if job["kind"] == "process" and await papers_store.dismissed_at(self.conn, papers[0]):
                state, why = "discarded", None
            else:
                why = error or failure(await failures.open_on(self.conn, papers, FAILED_RUNS))
                state = "failed" if why else "done"
            await jobs_store.finish(self.conn, job["id"], state, why)

    async def _chunk(self, job: dict[str, Any], run: ModelRun) -> dict[str, Any] | None:
        """The job's next chunk of papers through classification, analysis and grading; returns
        the job as recorded after it."""
        ids = job["scope"]["papers"][job["done"] : job["done"] + CHUNK]
        before = {k: v for k, v in run.counts.items() if isinstance(v, int)}
        await nightly.classify(self.conn, self.config, run, ids=ids)
        await nightly.analyse(self.conn, run, ids=ids)
        await nightly.grade(self.conn, self.config, run, ids=ids)
        counts = dict(job["counts"])
        for k, v in run.counts.items():
            if isinstance(v, int) and v != before.get(k, 0):
                counts[k] = counts.get(k, 0) + v - before.get(k, 0)
        return await jobs_store.progress(self.conn, job["id"], job["done"] + len(ids), counts)

    async def _rerun(self, job: dict[str, Any]) -> None:
        """A re-run's next chunk. One whose topic is no longer followed and active fails; one
        its ceiling stopped waits on the owner; one that ends lets go of the text passes it
        left (failed, or awaiting text too long) to the regular queue."""
        tree = await taxonomy_store.load(self.conn)
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
            if run.gateway.ceiling is None:
                run.gateway.ceiling = await reruns.ceiling(
                    self.conn, self.config, job, reruns.roles(topic)
                )
            chunk = await reruns.work(self.conn, self.config, run, job, tree, self.archive, CHUNK)
        except Exception as exc:
            log.exception("jobs: re-run %s failed", job["id"])
            await self._end_rerun(job, "failed", f"{type(exc).__name__}: {exc}")
            await self._close(job["id"], exc)
            return
        self.worked()
        match chunk.outcome:
            case "more":
                return
            case "waiting":  # its passes await their text: other jobs go first meanwhile
                now = datetime.now(self.config.schedule.zone)
                await jobs_store.wait(self.conn, job["id"], now + reruns.WAKE)
                return
            case "stopped":
                await jobs_store.finish(self.conn, job["id"], "stopped", chunk.where)
            case "done":
                why = f"grading failed on {chunk.failed}" if chunk.failed else None
                await self._end_rerun(job, "failed" if why else "done", why)
        await self._close(job["id"])

    async def _replay(self, job: dict[str, Any]) -> None:
        """A replay's next chunk, under its ceiling; one its ceiling stopped waits on the owner.
        Its run is an evaluation's: it observes nothing of the providers."""
        try:
            run = await self._run_of(job)
            if run.gateway.ceiling is None:
                run.gateway.ceiling = await replays.ceiling(self.conn, self.config, job)
            tried = self.open[job["id"]].tried
            chunk = await replays.work(self.conn, self.config, run, job, tried, CHUNK)
        except Exception as exc:
            log.exception("jobs: replay %s failed", job["id"])
            await jobs_store.finish(self.conn, job["id"], "failed", f"{type(exc).__name__}: {exc}")
            await self._close(job["id"], exc)
            return
        self.worked()
        match chunk.outcome:
            case "more":
                return
            case "stopped":
                await jobs_store.finish(self.conn, job["id"], "stopped", chunk.where)
            case "done":
                await jobs_store.finish(self.conn, job["id"], "done")
        await self._close(job["id"])

    async def _digest(self, job: dict[str, Any]) -> None:
        """A failed digest written again (`jobs.digests.again`), in one chunk."""
        try:
            run = await self._run_of(job)
            state, why = await digest_jobs.again(self.conn, run, job["scope"]["digest"])
            counts = {k: v for k, v in run.counts.items() if isinstance(v, int)}
            await jobs_store.progress(self.conn, job["id"], 1, counts)
            await jobs_store.finish(self.conn, job["id"], state, why)
        except Exception as exc:
            log.exception("jobs: writing digest %s again failed", job["scope"]["digest"])
            await jobs_store.finish(self.conn, job["id"], "failed", f"{type(exc).__name__}: {exc}")
            await self._close(job["id"], exc)
            return
        self.worked()
        await self._close(job["id"])

    async def _end_rerun(self, job: dict[str, Any], state: str, error: str | None) -> None:
        async with self.conn.transaction():
            await jobs_store.finish(self.conn, job["id"], state, error)
            await fulltext_store.release_job(self.conn, job["id"])

    async def _run_of(self, job: dict[str, Any]) -> ModelRun:
        """The run this worker records the job's work on: opened when it claims the job, kept
        across its chunks. Its gateway sends no batch, and answers a call a dead nightly left in
        an ended batch from that batch (`attaching`)."""
        if job["id"] not in self.open:
            stack = AsyncExitStack()
            factory = attaching(self.gateway)
            run = await stack.enter_async_context(
                model_run(
                    self.conn,
                    self.config,
                    "job",
                    gateway=factory,
                    job_id=job["id"],
                    evaluation=job["kind"] == "replay",
                )
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
        until taken back, once this session is gone."""
        for job_id in list(self.open):
            try:
                await self._close(job_id, error)
            except Exception:
                log.exception("jobs: closing job %s's run failed", job_id)


async def run_forever(
    dsn: str,
    config: Config,
    gateway: GatewayFactory = default_gateway,
    wake: asyncio.Event | None = None,
    worked: Callable[[], None] = lambda: None,
    archive: ArchiveClient | None = None,
) -> None:
    """Work the queue until cancelled: chunk after chunk while there is one to run, else look
    every `TICK_SECONDS` or as soon as `wake` is set (a job was queued). A lost database is
    retried on a new connection, whose session takes back the jobs of the old one."""
    wake = wake or asyncio.Event()
    log.info("jobs worker on")
    while True:
        try:
            async with await store.connect(dsn) as conn:
                worker = Worker(conn, config, gateway, worked, archive)
                try:
                    while True:
                        if not await worker.step(datetime.now(config.schedule.zone)):
                            with contextlib.suppress(TimeoutError):
                                await asyncio.wait_for(wake.wait(), TICK_SECONDS)
                            wake.clear()
                except BaseException as exc:
                    await worker.close(exc)
                    raise
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("jobs worker failed")
            await asyncio.sleep(TICK_SECONDS)
