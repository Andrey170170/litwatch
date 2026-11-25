"""The jobs Worker (docs/plans/jobs.md; docs/plans/a2-work-gate.md, P3): each work item of kind
`job` is one chunk of one job of the owner's queue, keyed by the job's id (`store.jobs`, which
submits it wherever the job becomes runnable). `litwatch serve` runs it under `[schedule]
enabled`.

A chunk works up to `CHUNK` papers under the run lock, and none starts while a nightly run is due
or running (`store.jobs.nightly_waits`, or a fire due that the scheduler has yet to publish,
`jobs.scheduler.unpublished`), read before the lock and again as it claims its job, under the
`fire` lock a fire's publication takes, so the nightly waits at most one chunk (pipeline.md,
section 10): such an item waits `WAIT` and looks again, as one that finds the run lock taken
does, unless the nightly brings it forward as it ends. Under the lock it claims its job, the
job's row locked, and works the chunk. One whose look before the claim fails ends as a chunk's
error does: the job failed, or, that not recorded, the chunk kept to look again. Each write to
the job's row arranges its next chunk in its own unit (`store.jobs.arrange`), so the chunk's
item ends with the decision of the job's last write; an error the chunk's own handling did not
record fails the job, which arranges none. One chunk runs
at a time, and the due one most urgent goes first: Process jobs and digests written again (the
owner waits on them), then the oldest in the queue. So a Process job queued during a long job
goes at its next chunk.

Each time this process runs a job, from its first chunk to its end, is one run of kind `job`,
however many chunks that takes: the Worker keeps it open between them, and it is alive while its
latest chunk's claim holds its lease (`store.runs.ALIVE`). A chunk whose item was taken back (its
process died), or that another process ran meanwhile, runs again on a run of its own (the job's
`run_id` names its latest), and its steps pick only what is still due; a run this process kept
for a job that moved on is closed. Closing a run only records it: a failure there is logged. A
chunk that loses the database, or a deadlock or a serialization conflict, raises it, and the
gate tries the item again; its other errors end the job failed.

A Process job runs the nightly's own steps (classification, analysis, grading) on its papers
only; each recorded result settles its paper as in the nightly, and the passes it queued are
then worked by their work items, as the owner's requests (`feedback.word.settle_many`).
A job is done once its papers have no step left failed; else it fails, and settles when they
do (`store.jobs.settle_process`).

A re-run (`jobs.reruns`) grades its scope for its topic, then works the text passes it queued,
through a gateway that holds its ceiling across its chunks: one stopped by it waits on the owner
as `stopped`. A replay (`jobs.replays`) works its papers the same way under its ceiling, writing
the evaluation tables only. A digest written again (`jobs.digests.again`) is one chunk, and goes
first, as a Process job does.
"""

from __future__ import annotations

import logging
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core import FAILED_RUNS
from litwatch.core.work import WorkItem
from litwatch.feedback import word
from litwatch.gates.database import BusyError, DatabaseGate, LeaseLostError, ReadUnit
from litwatch.gates.work import Later, conflicted, database_lost
from litwatch.jobs import digests as digest_jobs
from litwatch.jobs import nightly, replays, reruns, scheduler
from litwatch.jobs.context import GatewayFactory, ModelRun, attaching, default_gateway, model_run
from litwatch.quality import replay
from litwatch.store import failures
from litwatch.store import fulltext as fulltext_store
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store.calls import EVAL

log = logging.getLogger(__name__)

CHUNK = 50  # papers a chunk takes: the most the nightly waits behind a job
# How long a chunk that cannot start (a nightly due or running, the run lock taken, this
# process holding its most runs open) waits before it looks again.
WAIT = timedelta(seconds=30)
# The most runs this process holds open: re-runs whose passes await their text stay open while
# others run. Past it only the jobs it runs already and those the owner waits on start.
OPEN_MOST = 4


def failure(open_: dict[str, tuple[int, int]]) -> str | None:
    """Why a job whose papers have these failures left (`store.failures.open_on`) failed, or
    None: some paper's step failed, and the paper waits for the next run or, after three runs in
    a row, the owner."""
    failed = [
        f"{step} failed on {n}" + (" (waits on Needs you)" if stuck == n else "")
        for step, (n, stuck) in open_.items()
    ]
    return "; ".join(sorted(failed)) or None


def _retried(exc: BaseException) -> bool:
    """Whether the gate tries the chunk's item again for `exc` rather than the job failing: the
    database went away under it, a statement lost a conflict, or its lease is lost (the item
    is no longer this task's)."""
    return database_lost(exc) or conflicted(exc) or isinstance(exc, LeaseLostError)


@dataclass
class _Open:
    stack: AsyncExitStack
    run: ModelRun
    tried: set[str] = field(default_factory=set)  # a replay's papers this run has tried


class JobWorker:
    """Works the chunks of the owner's jobs through `db`, their calls through `gateway`'s
    models; a re-run's text passes read from `archive` (without, the re-run stops)."""

    def __init__(
        self,
        db: DatabaseGate,
        config: Config,
        gateway: GatewayFactory = default_gateway,
        archive: ArchiveClient | None = None,
    ) -> None:
        self.db, self.config, self.gateway, self.archive = db, config, gateway, archive
        self.open: dict[int, _Open] = {}  # the jobs this process runs, each with its run

    async def sweep(self) -> None:
        """Make sure each job that may run has its chunk's item, keeping one it has as it
        stands: each in a unit of its own, which locks the job's row and looks again."""
        async with self.db.read("jobs: open") as unit:
            found = await jobs_store.open_ids(unit)
        for job_id in found:
            async with self.db.write("jobs: sweep") as unit:
                await jobs_store.submit(unit, job_id, ensure=True)

    async def __call__(self, item: WorkItem) -> Later | None:
        waited = Later(datetime.now(UTC) + WAIT)
        try:
            return await self._chunk_for(item, waited)
        except Exception as exc:
            if _retried(exc):
                raise
            try:
                await self._fail(int(item.key), exc)
            except Exception:
                # Its end not recorded, the job may still run: the chunk is kept, to try again.
                log.exception("jobs: job %s's failure could not be recorded", item.key)
                return waited
            raise

    async def _chunk_for(self, item: WorkItem, waited: Later) -> Later | None:
        async with self.db.read("does the nightly wait") as unit:
            if await self._nightly_first(unit):
                return waited
        await self._evict()
        # The job is claimed under the run lock, so no other job's chunk, nor a nightly, runs
        # between claiming it and working it.
        held = AsyncExitStack()
        try:
            await held.enter_async_context(self.db.held("run"))
        except BusyError:
            return waited
        async with held:
            return await self._claimed(item, waited)

    async def _nightly_first(self, unit: ReadUnit) -> bool:
        """Whether a nightly goes before any chunk now: one due or running
        (`store.jobs.nightly_waits`), or a fire due that the scheduler has yet to publish."""
        return await jobs_store.nightly_waits(unit) or await scheduler.unpublished(
            unit, self.config
        )

    async def _claimed(self, item: WorkItem, waited: Later) -> Later | None:
        """Under the run lock: claim the item's job and work its chunk. The claim is decided
        under the `fire` lock, which publishing a fire takes, after the job's row is had: a
        nightly published, or due, by then goes first."""
        async with self.db.write("claim a job", locks=["fire"]) as unit:
            job = await jobs_store.get(unit, int(item.key), lock=True)
            if job is None or not await jobs_store.may_run(unit, job):
                if job is not None:
                    await jobs_store.arrange(unit, job["id"])
                return None  # ended, or not run, meanwhile: nothing to do
            if await self._nightly_first(unit):
                return waited
            if job["wake_at"] is not None and job["wake_at"] > datetime.now(UTC):
                return Later(job["wake_at"])
            mine = self._mine(job)
            ours = len(self.open) - (job["id"] in self.open)  # a stale one is closed below
            if not mine and ours >= OPEN_MOST and job["kind"] not in jobs_store.FIRST:
                return waited
            job = await jobs_store.claim(unit, job["id"], afresh=not mine)
        if not mine:  # one this process kept for the job is no longer its run
            await self._close(job["id"])
        await self._chunk_of(job, item)
        return None

    def _mine(self, job: dict[str, Any]) -> bool:
        """Whether the run this process keeps for the job is still the job's: running on it,
        no other process's chunk having run it since."""
        found = self.open.get(job["id"])
        return found is not None and job["state"] == "running" and job["run_id"] == found.run.run_id

    async def _evict(self) -> None:
        """Close the runs this process keeps for jobs that moved on without it (ended, put back,
        or run by another process since), so they hold none of its `OPEN_MOST`."""
        if not self.open:
            return
        async with self.db.read("jobs: still ours") as unit:
            jobs = {j: await jobs_store.get(unit, j) for j in self.open}
        for job_id, job in jobs.items():
            if job is None or not self._mine(job):
                await self._close(job_id)

    async def _fail(self, job_id: int, exc: Exception) -> None:
        """Fail the job whose chunk raised `exc`, which its handling did not record, if it may
        still run, letting go of a re-run's passes, and close its run: so its chunk's item ends
        with no next chunk, rather than leaving it running with none, or retrying it for good."""
        log.exception("jobs: job %s's chunk failed", job_id)
        async with self.db.write("job failed", taxonomy="shared") as unit:
            failed = await jobs_store.fail(unit, job_id, f"{type(exc).__name__}: {exc}")
            if failed is not None and failed["kind"] == "rerun":
                await fulltext_store.release_job(unit, job_id)
        await self._close(job_id, exc)

    async def _chunk_of(self, job: dict[str, Any], item: WorkItem) -> None:
        match job["kind"]:
            case "rerun":
                await self._rerun(job, item)
            case "replay":
                await self._replay(job, item)
            case "digest":
                await self._digest(job, item)
            case _:
                await self._process(job, item)

    async def _process(self, job: dict[str, Any], item: WorkItem) -> None:
        try:
            after = await self._chunk(job, await self._run_of(job, item))
        except Exception as exc:
            if _retried(exc):
                raise
            log.exception("jobs: job %s failed", job["id"])
            await self._end(job, f"{type(exc).__name__}: {exc}")
            await self._close(job["id"], exc)
            return
        if after is None:  # no longer running
            await self._close(job["id"])
        elif after["done"] >= after["papers"]:
            await self._end(job)
            await self._close(job["id"])

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
            await jobs_store.finish(unit, job["id"], state, why)

    async def _finish(self, job: dict[str, Any], state: str, error: str | None = None) -> None:
        async with self.db.write(f"job {state}") as unit:
            await jobs_store.finish(unit, job["id"], state, error)

    async def _chunk(self, job: dict[str, Any], run: ModelRun) -> dict[str, Any] | None:
        """The job's next chunk of papers through classification, analysis and grading; returns
        the job as recorded after it."""
        ids = job["scope"]["papers"][job["done"] : job["done"] + CHUNK]
        before = run.chunk_began()
        await nightly.classify(self.db, self.config, run, ids=ids)
        await nightly.analyse(self.db, self.config, run, ids=ids)
        await nightly.grade(self.db, self.config, run, ids=ids)
        counts = Counter(job["counts"]) + (run.tally() - before)
        done = job["done"] + len(ids)
        async with self.db.write("job progress") as unit:
            after = await jobs_store.progress(unit, job["id"], done, dict(counts))
        run.chunk_recorded()
        return after

    async def _ceiling(self, job: dict[str, Any], run: ModelRun, roles: tuple[str, ...]) -> None:
        if run.gateway.ceiling is None:
            async with self.db.read("job ceiling") as unit:
                run.gateway.ceiling = await reruns.ceiling(unit, self.config, job, roles)

    async def _rerun(self, job: dict[str, Any], item: WorkItem) -> None:
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
            run = await self._run_of(job, item)
            await self._ceiling(job, run, reruns.roles(topic))
            chunk = await reruns.work(self.db, self.config, run, job, tree, self.archive, CHUNK)
        except Exception as exc:
            if _retried(exc):
                raise
            log.exception("jobs: re-run %s failed", job["id"])
            await self._end_rerun(job, "failed", f"{type(exc).__name__}: {exc}")
            await self._close(job["id"], exc)
            return
        match chunk.outcome:
            case "more":
                return
            case "waiting":  # its passes await their text: other jobs go first meanwhile
                async with self.db.write("re-run waits") as unit:
                    await jobs_store.wait(unit, job["id"], datetime.now(UTC) + reruns.WAKE)
                return
            case "stopped":
                await self._finish(job, "stopped", chunk.where)
            case "done":
                why = f"grading failed on {chunk.failed}" if chunk.failed else None
                await self._end_rerun(job, "failed" if why else "done", why)
        await self._close(job["id"])

    async def _replay(self, job: dict[str, Any], item: WorkItem) -> None:
        """A replay's next chunk, under its ceiling; one its ceiling stopped waits on the owner.
        Its run is an evaluation's: it observes nothing of the providers."""
        try:
            run = await self._run_of(job, item)
            await self._ceiling(job, run, replay.ROLES)
            tried = self.open[job["id"]].tried
            chunk = await replays.work(self.db, self.config, run, job, tried, CHUNK)
        except Exception as exc:
            if _retried(exc):
                raise
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

    async def _digest(self, job: dict[str, Any], item: WorkItem) -> None:
        """A failed digest written again (`jobs.digests.again`), in one chunk."""
        try:
            run = await self._run_of(job, item)
            state, why = await digest_jobs.again(
                self.db, run, job["scope"]["digest"], rules=word.rules(self.config)
            )
            async with self.db.write("digest job") as unit:
                await jobs_store.progress(unit, job["id"], 1, dict(run.tally()))
                await jobs_store.finish(unit, job["id"], state, why)
        except Exception as exc:
            if _retried(exc):
                raise
            await self._failed(job, exc)
            return
        await self._close(job["id"])

    async def _failed(self, job: dict[str, Any], exc: Exception) -> None:
        """End a job that raised as failed, with its error, and close its run with it."""
        log.exception("jobs: %s job %s failed", job["kind"], job["id"])
        await self._finish(job, "failed", f"{type(exc).__name__}: {exc}")
        await self._close(job["id"], exc)

    async def _end_rerun(self, job: dict[str, Any], state: str, error: str | None) -> None:
        """End the re-run and let go of its passes, in one unit, if it is still running."""
        async with self.db.write("end re-run", taxonomy="shared") as unit:
            if await jobs_store.finish(unit, job["id"], state, error):
                await fulltext_store.release_job(unit, job["id"])

    async def _run_of(self, job: dict[str, Any], item: WorkItem) -> ModelRun:
        """The run this process records the job's work on, of kind `job` (`eval` for a replay):
        opened at the job's first chunk here, kept across its chunks, each of whose items it is
        alive by. Its gateway sends no batch, and answers a call a dead nightly left in an ended
        batch from that batch (`attaching`); a replay's, as the CLI's, leaves other runs'
        batches alone."""
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
        run = self.open[job["id"]].run
        async with self.db.write("job: the chunk's run") as unit:
            await jobs_store.link(unit, job["id"], item, run.run_id)
        return run

    async def _close(self, job_id: int, error: BaseException | None = None) -> None:
        """Close the run this process keeps for the job, with the error that ended it. It only
        records the run's end, the job's own writes having arranged what comes next, so a failure
        here (the database lost) is logged, and the run left open, no longer alive."""
        found = self.open.pop(job_id, None)
        if found is None:
            return
        try:
            if error is None:
                await found.stack.aclose()
            else:
                await found.stack.__aexit__(type(error), error, error.__traceback__)
        except Exception:
            log.exception("jobs: closing job %s's run failed", job_id)

    async def close(self, error: BaseException) -> None:
        """Close every run this process has open, as ended by `error` (the service stopping):
        their jobs stay running, and their chunks' items, given back, go on with them on runs
        of their own."""
        for job_id in list(self.open):
            await self._close(job_id, error)
