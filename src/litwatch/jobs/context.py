"""What a job runs inside: a run record, and for a job that calls models a journal and a
gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg

from litwatch import llm, store
from litwatch.config import Config
from litwatch.store import runs
from litwatch.store.calls import CallJournal

# Tests swap this for a gateway over scripted providers.
GatewayFactory = Callable[[Config, CallJournal], llm.Gateway]


def default_gateway(config: Config, journal: CallJournal) -> llm.Gateway:
    return llm.build(config.models, journal, debug=config.debug)


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


@asynccontextmanager
async def run_record(
    conn: psycopg.AsyncConnection, kind: str, *, window: tuple[datetime, datetime] | None = None
) -> AsyncIterator[RunRecord]:
    """Open a run and close it with what the job recorded on it and the error that ended it,
    whatever happens inside, a cancelled job included."""
    record = RunRecord(await runs.start(conn, kind, *(window or (None, None))))
    try:
        yield record
    except BaseException as exc:
        record.fields["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        await runs.finish(conn, record.run_id, counts=record.counts, **record.fields)


@dataclass
class ModelRun:
    record: RunRecord
    gateway: llm.Gateway
    journal: CallJournal

    @property
    def run_id(self) -> int:
        return self.record.run_id

    @property
    def counts(self) -> dict[str, Any]:
        return self.record.counts

    def add(self, name: str, n: int = 1) -> None:
        self.record.add(name, n)


@asynccontextmanager
async def model_run(
    conn: psycopg.AsyncConnection,
    config: Config,
    kind: str,
    *,
    window: tuple[datetime, datetime] | None = None,
    gateway: GatewayFactory = default_gateway,
) -> AsyncIterator[ModelRun]:
    """A run record (`run_record`) that also closes with the calls' cost and counts by role.

    The journal writes on a connection of its own, since calls finish while the job's
    connection is inside a transaction.
    """
    async with run_record(conn, kind, window=window) as record, await store.sibling(conn) as side:
        journal = CallJournal(side, record.run_id)
        try:
            yield ModelRun(record, gateway(config, journal), journal)
        finally:
            record.counts["calls"] = await journal.by_role()
            record.fields["cost"] = round(await journal.total(), 4)
