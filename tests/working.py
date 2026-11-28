"""A Work gate in a test: waiting on what must happen (`until`), serving a gate's Workers for a
block (`serving`), reading a key's work items back (`items_of`), and running the jobs' chunks
through a gate (`chunks`, `step`, `work_out`), or killed in one (`killed`), beside a scheduled
nightly (`nightly_due`); work run as a work item (`in_an_item`); `litwatch serve`'s app with
its Work gate running (`served`); and a `litwatch` command through its real CLI (`command`)."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Mapping, Sequence
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner
from typer.testing import Result as Ran

from litwatch.api import create_app
from litwatch.config import Config, Schedule
from litwatch.core.work import WorkItem
from litwatch.gates import work as work_gate
from litwatch.gates.database import DatabaseGate, LeaseLostError
from litwatch.gates.work import Result, Worker, WorkGate
from litwatch.jobs import commands
from litwatch.jobs.worker import JobWorker
from litwatch.store import jobs as jobs_store
from litwatch.store import work as queue
from tests.sessions import Bridge, Session, in_read

WAIT = 10.0  # how long a test waits on something that must happen
# The schedule on, but no fire near now (29 February only), so `serve` submits none.


def quiet() -> Schedule:
    """The schedule on (the workers run) with no fire near the tests' run, whatever the date:
    daily, half a day from the time they start, so the last fire is past its catch-up window
    (`jobs.scheduler.CATCH_UP`) and the next hours away; its deadline a minute after its time,
    passed, so a nightly does not batch."""
    zone = Schedule().zone
    at = datetime.now(zone) + timedelta(hours=12)
    at = at.replace(minute=min(at.minute, 58))  # its deadline on the same day
    cron = f"{at.minute} {at.hour} * * *"
    return Schedule(
        enabled=True, nightly=cron, publications=cron, deadline=time(at.hour, at.minute + 1)
    )


QUIET = quiet()
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
    upkeep: Sequence[Callable[[], Awaitable[None]]] = (),
) -> AsyncGenerator[asyncio.Task]:
    """`gate.run(workers, keys=keys, upkeep=upkeep)` for the block, listening and asleep before
    it starts; stopped at the end."""
    task = asyncio.create_task(gate.run(workers, keys=keys, upkeep=upkeep))

    async def idle() -> bool:
        return gate.listening.is_set() and gate.idle

    try:
        await until(idle, "the gate listens and sleeps")
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class StoppedError(Exception):
    """The item `in_an_item` ran was stopped: its lease was lost."""


async def in_an_item[T](
    db: DatabaseGate, name: str, work: Callable[[], Awaitable[T]], *, admission: str | None = None
) -> T:
    """`work` run as `serve` runs a work item: the item `test:<name>` (key `name`, admitted by
    `admission`, by default its key) on a Work gate of its own, which renews its lease; what
    `work` returns or raises; `StoppedError` when the gate stopped it."""
    kind = f"test:{name}"
    ended: asyncio.Future[T] = asyncio.get_running_loop().create_future()

    async def worker(_: WorkItem) -> None:
        try:
            found = await work()
        except (asyncio.CancelledError, LeaseLostError):  # stopped, then its fenced writes fail
            if not ended.done():
                ended.set_exception(StoppedError(f"{kind} was stopped"))
            raise
        except BaseException as exc:
            if not ended.done():
                ended.set_exception(exc)
            raise
        ended.set_result(found)

    async with db.write("test: the item") as unit:
        await queue.submit(unit, kind, name, admission=admission or name)
    # Its lease the module's as it is now: a test may shorten it.
    gate = WorkGate(db, lease=work_gate.LEASE)
    async with serving(gate, {kind: worker}, keys={kind: [name]}):
        return await ended


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
    """`gone`'s process is killed at its method `at`: it stops there, gives nothing back, and
    its chunk's item stays running until its lease lapses (made to here at once), so the next
    gate takes it back; the run it had open stays open (`gone.close` ends it later)."""
    reached = asyncio.Event()

    async def dies(*_):
        reached.set()
        await asyncio.Event().wait()  # never set: the process is gone

    async def nothing(_: WorkItem) -> None:
        pass  # a killed process gives no claim back

    monkeypatch.setattr(gone, at, dies)
    gate = WorkGate(gone.db)
    monkeypatch.setattr(gate, "_give_back", nothing)
    task = asyncio.create_task(gate.run({jobs_store.WORK: gone}))

    async def stopped() -> bool:
        return reached.is_set()

    try:
        await until(stopped, f"the chunk to reach {at}")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    async with gone.db.write("test: the dead process's lease lapses") as unit:
        found = await unit.execute(
            "UPDATE work_items SET lease_until = now() - interval '1 second' "
            "WHERE kind = %s AND state = 'running'",
            (jobs_store.WORK,),
        )
        assert found.rowcount == 1


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


async def stranded(conn: Session) -> list[int]:
    """The jobs that may run but have no open chunk item: nothing would ever run them."""
    runnable = await in_read(conn, jobs_store.open_ids)
    rows = await (
        await conn.execute(
            "SELECT key FROM work_items WHERE kind = %s AND state <> 'ended'", (jobs_store.WORK,)
        )
    ).fetchall()
    return sorted(set(runnable) - {int(r["key"]) for r in rows})


async def on_tonight(conn: Session, job_id: int) -> None:
    """Put the job not run on tonight's run, as With tonight's run does: under the `tonight`
    lock."""
    async with Bridge(conn).write("with tonight's run", locks=["tonight"]) as unit:
        assert await jobs_store.move(unit, job_id, jobs_store.NOT_RUN, "tonight") is not None


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
    the sweeps, then one Work gate with the Workers `config` enables, registered (a command
    finds it serves) as the startup ends; stopped at the end, as `serve` stops. `kwargs` go to
    `create_app` (`models`, `publications`, `archive`)."""
    app = create_app("postgresql://unused", TOKEN, config, db=db, **kwargs)
    async with app.router.lifespan_context(app):
        yield app


# A command's configuration when the test gives none: the defaults, the zone pinned.
CONFIG_TOML = '[schedule]\ntimezone = "UTC"\n'


async def command(
    conn: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *argv: str,
    config: str | None = CONFIG_TOML, input: str | None = None,
) -> Ran:  # fmt: skip
    """The `litwatch` command `argv` through its real CLI, in a thread of its own (it runs its
    own event loop), on the test's schema, with a configuration file of `config` (None: none),
    as $LITWATCH_CONFIG, and `input` its answers: in `tmp_path`, as every command loads ./.env."""
    from litwatch import cli  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "LITWATCH_DATABASE_URL", f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    )
    if config is not None:
        path = tmp_path / "litwatch.toml"
        path.write_text(config)
        monkeypatch.setenv("LITWATCH_CONFIG", str(path))
    return await asyncio.to_thread(CliRunner().invoke, cli.app, list(argv), input=input)


async def night(db: DatabaseGate, conn: Session) -> None:
    """The nightly as `litwatch nightly` runs it, without fetching or digests: an item
    (`jobs.commands`) the test's `serve` works, through to its end."""
    name = str(uuid4())
    scope = {"fetch": False, "days": None, "limit": None, "digests": False}
    async with db.write("litwatch nightly") as unit:
        item = await queue.submit(unit, commands.kind("nightly"), name, scope=scope, admission=name)

    async def ended() -> bool:
        return (await state_of(conn, item.id))["state"] == "ended"

    await until(ended, "the nightly ends")
    assert (await state_of(conn, item.id))["outcome"] == "done"


async def state_of(conn: Session, item_id: int) -> dict[str, Any]:
    found = await (
        await conn.execute(
            "SELECT state, outcome, error, cost, attempts FROM work_items WHERE id = %s",
            (item_id,),
        )
    ).fetchone()
    assert found is not None
    return found
