"""What a job runs inside: a run record, and for a job that calls models a journal and a
gateway."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from litwatch import llm
from litwatch.config import Config
from litwatch.gates.database import DatabaseGate, Named, WriteUnit
from litwatch.store import runs
from litwatch.store.calls import EVAL, CallJournal

log = logging.getLogger(__name__)

# Tests swap this for a gateway over scripted providers.
GatewayFactory = Callable[[Config, CallJournal], llm.Gateway]
# What a job writes in the unit that closes its run, and the locks that unit takes for it.
Closing = tuple[Callable[[WriteUnit], Awaitable[None]], Sequence[Named]]


def default_gateway(config: Config, journal: CallJournal) -> llm.Gateway:
    return llm.build(config.models, journal, debug=config.debug)


def attaching(factory: GatewayFactory) -> GatewayFactory:
    """`factory`'s gateways, sending no batch but answering a call a dead run left in a batch
    that has ended from that batch rather than paying for it again: the service's workers, which
    run outside the nightly's batch window. A batch still running is past that run's deadline:
    it is cancelled and given up at once (no drain), so a worker never waits on it."""

    def gateway(config: Config, journal: CallJournal) -> llm.Gateway:
        found = factory(config, journal)
        found.batch_until(datetime.now(UTC), drain=timedelta(0))
        return found

    return gateway


@dataclass
class RunRecord:
    """A run as a job records it, to be closed with: its counts, and its other fields as
    `runs.finish` takes them (fetched, inserted, updated, cost, intake; an error the job
    reports without raising)."""

    run_id: int
    counts: dict[str, Any] = field(default_factory=dict)
    fields: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n


class UnclosedError(RuntimeError):
    """A run whose close carries a write (`closing`) did not close: that unit failed, so
    neither the run's end nor its write stands. Raised from the close's error, in place of the
    job's own when the job failed first (`failure`), so its caller knows the run is still open
    and the write left to do: the scheduled nightly tries its fire again."""

    def __init__(self, run_id: int, failure: BaseException | None) -> None:
        super().__init__(f"run {run_id} did not close; its jobs wait for it")
        self.run_id, self.failure = run_id, failure


@asynccontextmanager
async def run_record(
    db: DatabaseGate,
    kind: str,
    *,
    window: tuple[datetime, datetime] | None = None,
    job_id: int | None = None,
    closing: Closing | None = None,
) -> AsyncIterator[RunRecord]:
    """Open a run, alive while the work item it runs in holds its lease (`store.runs.ALIVE`),
    and close it with what the job recorded on it and the error that ended it, whatever happens
    inside, a cancelled job included. Each is a unit of its own; `closing` is written in the
    closing one, under its locks, so it stands or falls with the run's end (the nightly's
    release of the jobs it held back): when that unit fails, `UnclosedError` is raised."""
    async with db.write(f"open {kind} run") as unit:
        run_id = await runs.start(unit, kind, *(window or (None, None)), job_id=job_id)
    record = RunRecord(run_id)
    failure: BaseException | None = None
    try:
        yield record
    except BaseException as exc:
        failure = exc
        record.fields["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            await _finish(db, record, closing)
        except BaseException as exc:
            # A failure here after the job's (a cancellation included) is logged, so the job's
            # error is the one recorded and raised; but not a close that carried a write.
            if closing is not None and isinstance(exc, Exception):
                raise UnclosedError(record.run_id, failure) from exc
            if failure is None:
                raise
            log.exception("run %s: closing it failed after the job did", record.run_id)


async def _finish(db: DatabaseGate, record: RunRecord, closing: Closing | None) -> None:
    write, locks = closing or (None, ())
    async with db.write("close run", locks=locks) as unit:
        await runs.finish(unit, record.run_id, counts=record.counts, **record.fields)
        if write is not None:
            await write(unit)


async def _closing(step: Awaitable[None], failed: bool, run_id: int, what: str) -> None:
    """Await a step that closes a run. If the job failed already, a failure here (a cancellation
    included) is logged and not raised, so the error that ended the job is the one recorded and
    raised."""
    try:
        await step
    except BaseException:
        if not failed:
            raise
        log.exception("run %s: %s failed after the job did", run_id, what)


@dataclass
class ModelRun:
    record: RunRecord
    gateway: llm.Gateway
    journal: CallJournal
    began: Counter[str] | None = None  # `chunk_began`

    @property
    def run_id(self) -> int:
        return self.record.run_id

    @property
    def counts(self) -> dict[str, Any]:
        return self.record.counts

    def add(self, name: str, n: int = 1) -> None:
        self.record.add(name, n)

    def tally(self) -> Counter[str]:
        """What the run has counted so far, its numbers alone: a job adds what a chunk counted
        (`tally()` after it less before) to its own counts."""
        return Counter({k: v for k, v in self.counts.items() if isinstance(v, int)})

    def chunk_began(self) -> Counter[str]:
        """What the run had counted as the job's chunk under way began (`tally`), which the
        chunk's counts are taken from: kept until its progress is recorded (`chunk_recorded`),
        so a chunk tried again after a lost database counts what its first try recorded too,
        whose steps it then skips."""
        if self.began is None:
            self.began = self.tally()
        return self.began

    def chunk_recorded(self) -> None:
        """The chunk's progress is recorded: the next one counts from here."""
        self.began = None


@asynccontextmanager
async def model_run(
    db: DatabaseGate,
    config: Config,
    kind: str,
    *,
    window: tuple[datetime, datetime] | None = None,
    gateway: GatewayFactory = default_gateway,
    job_id: int | None = None,
    closing: Closing | None = None,
) -> AsyncIterator[ModelRun]:
    """A run record (`run_record`, with `closing`) that also closes with the calls' cost and
    counts by role. Its journal writes each call in units of its own on `db`."""
    async with run_record(db, kind, window=window, job_id=job_id, closing=closing) as record:
        # An evaluation run writes its own tables only (AGENTS.md): no provider observations.
        journal = CallJournal(db, record.run_id, observes=kind != EVAL)
        failed = True
        try:
            yield ModelRun(record, gateway(config, journal), journal)
            failed = False
        finally:
            await _closing(_tally(record, journal), failed, record.run_id, "reading its journal")


async def _tally(record: RunRecord, journal: CallJournal) -> None:
    record.fields["cost"] = round(await journal.total(), 4)  # first: what was spent matters most
    record.counts["calls"] = await journal.by_role()
