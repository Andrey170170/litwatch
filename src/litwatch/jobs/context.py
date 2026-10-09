"""What a job that calls models runs inside: a run record, a journal and a gateway."""

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
    return llm.build(config.models, journal)


@dataclass
class ModelRun:
    run_id: int
    gateway: llm.Gateway
    journal: CallJournal
    counts: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n


@asynccontextmanager
async def model_run(
    conn: psycopg.AsyncConnection,
    config: Config,
    kind: str,
    *,
    window: tuple[datetime, datetime] | None = None,
    gateway: GatewayFactory = default_gateway,
) -> AsyncIterator[ModelRun]:
    """Open a run and close it with its counts, cost and error, whatever happens inside.

    The journal writes on a connection of its own, since calls finish while the job's
    connection is inside a transaction.
    """
    run_id = await runs.start(conn, kind, *(window or (None, None)))
    async with await store.sibling(conn) as side:
        journal = CallJournal(side, run_id)
        run = ModelRun(run_id, gateway(config, journal), journal)
        error = None
        try:
            yield run
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            run.counts["calls"] = await journal.by_role()
            await runs.finish(
                conn, run_id, cost=round(await journal.total(), 4), counts=run.counts, error=error
            )
