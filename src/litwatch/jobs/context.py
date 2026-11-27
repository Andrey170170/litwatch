"""What a job runs inside: a run record, and for a job that calls models its journal and its
door to the process's one Model gate (`llm.ModelGate`, built by `model_gate`)."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from litwatch import llm
from litwatch.config import Config
from litwatch.gates.database import DatabaseGate, Named, WriteUnit
from litwatch.store import runs
from litwatch.store.calls import EVAL, CallJournal, tally

log = logging.getLogger(__name__)

# What a job writes in the unit that closes its run, and the locks that unit takes for it.
Closing = tuple[Callable[[WriteUnit], Awaitable[None]], Sequence[Named]]


def model_gate(db: DatabaseGate, config: Config) -> llm.ModelGate:
    """The process's Model gate, over the providers with credentials, its own journal on `db`:
    built once by `serve`, which closes it (`async with`); a CLI command has `serve` run its
    work."""
    return llm.build(config.models, CallJournal(db), debug=config.debug)


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
    tallied: bool = False,
) -> AsyncIterator[RunRecord]:
    """Open a run, alive while the work item it runs in holds its lease (`store.runs.ALIVE`),
    and close it with what the job recorded on it and the error that ended it, whatever happens
    inside, a cancelled job included. Each is a unit of its own; `closing` is written in the
    closing one, under its locks, so it stands or falls with the run's end (the nightly's
    release of the jobs it held back): when that unit fails, `UnclosedError` is raised. A run
    that calls models (`tallied`) closes with its cost and calls by role from its journal's
    rows, under its `retally` lock (`store.calls.tally`)."""
    async with db.write(f"open {kind} run") as unit:
        run_id = await runs.start(unit, kind, *(window or (None, None)), job_id=job_id)
    record = RunRecord(run_id)
    failure: BaseException | None = None
    try:
        yield record
    except BaseException as exc:
        failure = exc
        record.fields["error"] = _error(exc)
        raise
    finally:
        try:
            await _finish(db, record, closing, tallied)
        except BaseException as exc:
            # A failure here after the job's (a cancellation included) is logged, so the job's
            # error is the one recorded and raised; but not a close that carried a write.
            if closing is not None and isinstance(exc, Exception):
                raise UnclosedError(record.run_id, failure) from exc
            if failure is None:
                raise
            log.exception("run %s: closing it failed after the job did", record.run_id)


def _error(exc: BaseException) -> str:
    """The error a run records: a cancellation's own message when it has one (the Work gate's
    reason, `serve` stopping or the owner's cancel), else the exception's type and text."""
    if isinstance(exc, asyncio.CancelledError) and str(exc):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


async def _finish(
    db: DatabaseGate, record: RunRecord, closing: Closing | None, tallied: bool
) -> None:
    write, locks = closing or (None, ())
    retally: list[Named] = [("retally", record.run_id)] if tallied else []
    async with db.write("close run", locks=[*locks, *retally]) as unit:
        await runs.finish(unit, record.run_id, counts=record.counts, **record.fields)
        if tallied:
            await tally(unit, record.run_id)
        if write is not None:
            await write(unit)


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
    kind: str,
    *,
    models: llm.ModelGate,
    window: tuple[datetime, datetime] | None = None,
    job_id: int | None = None,
    closing: Closing | None = None,
) -> AsyncIterator[ModelRun]:
    """A run record (`run_record`, with `closing`) whose calls go through `models`, the
    process's Model gate, journalled on the run, each in units of its own on `db`; it closes
    with their cost and counts by role."""
    async with run_record(
        db, kind, window=window, job_id=job_id, closing=closing, tallied=True
    ) as record:
        # An evaluation run writes its own tables only (AGENTS.md): no provider observations.
        journal = CallJournal(db, record.run_id, observes=kind != EVAL)
        yield ModelRun(record, models.on(journal), journal)
