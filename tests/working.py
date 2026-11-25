"""A Work gate in a test: waiting on what must happen (`until`), serving a gate's Workers for a
block (`serving`), reading a key's work items back (`items_of`), and running the jobs' chunks
through a gate (`chunks`, `step`, `work_out`), or killed in one (`killed`), beside a scheduled
nightly (`nightly_due`); and `litwatch serve`'s app with its Work gate running (`served`)."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Mapping
from typing import Any

import pytest
from fastapi import FastAPI

from litwatch.api import create_app
from litwatch.config import Config, Schedule
from litwatch.core.work import WorkItem
from litwatch.gates.database import DatabaseGate, LeaseLostError
from litwatch.gates.work import Result, Worker, WorkGate
from litwatch.jobs.worker import JobWorker
from litwatch.store import jobs as jobs_store
from tests.sessions import Session, in_read

WAIT = 10.0  # how long a test waits on something that must happen
# The schedule on, but no fire near now (29 February only), so `serve` submits none.
QUIET = Schedule(enabled=True, nightly="0 0 29 2 *", publications="0 0 29 2 *")
TOKEN = "test"  # the API token of the app `served` runs


async def until(check: Callable[[], Awaitable[bool]], what: str) -> None:
    """Wait until `check` holds, looking again every 50 ms, for at most `WAIT`; a timeout says
    `what` it waited for."""
    try:
        async with asyncio.timeout(WAIT):
            while not await check():
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise TimeoutError(f"waited {WAIT:.0f}s for: {what}") from None


@contextlib.asynccontextmanager
async def serving(
    gate: WorkGate,
    workers: Mapping[str, Worker],
    keys: Mapping[str, Collection[str]] | None = None,
) -> AsyncGenerator[asyncio.Task]:
    """`gate.run(workers, keys=keys)` for the block, listening and asleep before it starts;
    stopped at the end."""
    task = asyncio.create_task(gate.run(workers, keys=keys))

    async def idle() -> bool:
        return gate.listening.is_set() and gate.idle

    try:
        await until(idle, "the gate listens and sleeps")
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def items_of(conn: Session, kind: str, key: str) -> list[dict[str, Any]]:
    """The work items of `kind` and `key`, oldest first."""
    return await (
        await conn.execute(
            "SELECT * FROM work_items WHERE kind = %s AND key = %s ORDER BY id", (kind, key)
        )
    ).fetchall()


async def chunks(
    worker: JobWorker, limit: int | None = None
) -> list[Result | None | BaseException]:
    """Serve `worker`'s job chunks on a Work gate of its own on the worker's database until no
    chunk is running or due, or `limit` chunks' items have run; how each ended, in order (None:
    done, a chunk worked or nothing to do; `Later`: it could not start; what it raised). An item
    past `limit` is held until the gate stops, which gives it back."""
    results: list[Result | None | BaseException] = []
    ran: list[int] = []
    # The items held past `limit` (one given back may be held again), and those whose lease
    # was lost: no longer this gate's to settle.
    elsewhere: list[int] = []
    held = asyncio.Event()  # never set

    async def counted(item: WorkItem) -> Result | None:
        if limit is not None and len(ran) >= limit:
            elsewhere.append(item.id)
            await held.wait()
        ran.append(item.id)
        try:
            result = await worker(item)
        except BaseException as exc:
            results.append(exc)
            if isinstance(exc, LeaseLostError):
                elsewhere.append(item.id)
            raise
        results.append(result)
        return result

    gate = WorkGate(worker.db)

    async def out() -> bool:
        async with worker.db.read("test: chunks left") as unit:
            row = await (
                await unit.execute(
                    "SELECT count(*) FILTER (WHERE id = ANY(%(ran)s) AND state = 'running' "
                    "AND id <> ALL(%(elsewhere)s)) AS mine, "
                    "count(*) FILTER (WHERE state = 'running' OR not_before <= now() "
                    "AND state = 'pending') AS left FROM work_items WHERE kind = %(kind)s",
                    {"ran": ran, "elsewhere": elsewhere, "kind": jobs_store.WORK},
                )
            ).fetchone()
        assert row is not None
        if limit is not None and len(ran) >= limit:
            return len(results) == len(ran) and row["mine"] == 0
        return row["left"] == 0 and gate.idle

    async with serving(gate, {jobs_store.WORK: counted}):
        await until(out, "the jobs' chunks to run out")
    return results


async def killed(gone: JobWorker, monkeypatch: pytest.MonkeyPatch, at: str) -> None:
    """`gone`'s process is killed at its method `at`: its chunk's item, running, is given back
    as its gate stops, and the run it had open stays open (`gone.close` ends it later)."""

    async def dies(*_):
        raise asyncio.CancelledError

    monkeypatch.setattr(gone, at, dies)
    (ended,) = await chunks(gone, limit=1)
    assert isinstance(ended, asyncio.CancelledError)


async def step(worker: JobWorker) -> bool:
    """Run the next chunk due through the gate, if any: whether it worked one (it ended done,
    not waiting)."""
    found = await chunks(worker, limit=1)
    return bool(found) and found[0] is None


async def work_out(worker: JobWorker, most: int = 20) -> int:
    """Run job chunks through the gate until none is due; how many ended done."""
    found = await chunks(worker)
    assert len(found) <= most, "the chunks never ran out"
    return sum(r is None for r in found)


async def job_of(conn: Session, job_id: int) -> dict[str, Any]:
    """The job as stored, which must exist."""
    found = await in_read(conn, jobs_store.get, job_id)
    assert found is not None
    return found


async def nightly_due(conn: Session) -> None:
    """A scheduled nightly fire waits to run: its item is pending."""
    await conn.execute(
        "INSERT INTO work_items (kind, key, admission, lasting) "
        "VALUES ('nightly', 'a fire', 'nightly', true)"
    )


async def nightly_done(conn: Session) -> None:
    """The scheduled nightly fire's item ended."""
    await conn.execute(
        "UPDATE work_items SET state = 'ended', outcome = 'done' WHERE kind = 'nightly'"
    )


@contextlib.asynccontextmanager
async def served(db: DatabaseGate, config: Config, **kwargs: Any) -> AsyncGenerator[FastAPI]:
    """`litwatch serve`'s app on `db` (open, the caller's) for the block, its lifespan running:
    the sweeps, then one Work gate with the Workers `config` enables; stopped at the end, as
    `serve` stops. `kwargs` go to `create_app` (`gateway`, `publications`, `archive`)."""
    app = create_app("postgresql://unused", TOKEN, config, db=db, **kwargs)
    async with app.router.lifespan_context(app):
        yield app
