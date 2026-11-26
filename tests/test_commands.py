"""The CLI's commands as work items (docs/plans/a2-work-gate.md, P3): each runs its work as an
item of kind `command` in its own process (`gates.work.inline`), whose lease keeps its runs
alive; one attempt, a lost database ending it failed, and its item settled however it stops;
one that does what a kind of `serve`'s does takes that kind's admission, so the two run one at a
time; and one whose process died is ended by any `serve`'s take-back. On real Postgres."""

import asyncio
from datetime import timedelta

import psycopg
import pytest

from litwatch.core.work import ItemState, Outcome
from litwatch.gates.context import current_work
from litwatch.gates.work import CommandCutOffError, WorkGate, inline
from litwatch.store import fulltext as fulltext_store
from litwatch.store import runs
from litwatch.store import work as queue
from tests.sessions import Session
from tests.working import command, serving


async def commands(conn: Session) -> list[dict]:
    return await (
        await conn.execute("SELECT * FROM work_items WHERE kind = 'command' ORDER BY id")
    ).fetchall()


async def alive(conn: Session, run_id: int) -> bool:
    query = f"SELECT {runs.ALIVE} AS alive FROM runs r WHERE r.id = %s"
    row = await (await conn.execute(query, (run_id,))).fetchone()
    return row["alive"]


async def test_a_command_runs_as_an_item_of_its_own_whose_lease_keeps_its_run_alive(
    conn: Session, db
):
    seen = {}

    async def work() -> str:
        seen["work"] = current_work()
        async with db.write("the command's run") as unit:
            seen["run"] = await runs.start(unit, "nightly")
        seen["alive"] = await alive(conn, seen["run"])
        async with db.write("closed") as unit:
            await runs.finish(unit, seen["run"])
        return "its result"

    assert await inline(db, "nightly", work) == "its result"
    (item,) = await commands(conn)
    assert item["key"].startswith("nightly:") and item["admission"] == item["key"]
    assert (item["state"], item["outcome"]) == ("ended", "done")
    assert seen["work"] is not None and seen["work"].item.id == item["id"]
    assert seen["alive"] and not await alive(conn, seen["run"])
    run = await (await conn.execute("SELECT * FROM runs WHERE id = %s", (seen["run"],))).fetchone()
    assert run["work_id"] == item["id"]
    assert run["backend_pid"] is not None  # the columns the image before the gate reads


async def test_a_run_is_alive_by_its_claim_and_not_revived_when_its_item_is_taken_again(
    conn: Session, db
):
    """The item's process died; `serve` takes it back and leases it again, the same item under
    a new claim: the run the dead claim opened stays dead."""
    seen = {}

    async def work() -> None:
        async with db.write("the command's run") as unit:
            seen["run"] = await runs.start(unit, "nightly")

    await inline(db, "nightly", work)
    assert not await alive(conn, seen["run"])
    await conn.execute(  # the item running again, as the dead claim had it
        "UPDATE work_items SET state = 'running', outcome = NULL, lease_until = now() + "
        "interval '1 hour', lease_token = (SELECT lease_token FROM runs WHERE id = %s)",
        (seen["run"],),
    )
    assert await alive(conn, seen["run"])
    await conn.execute("UPDATE work_items SET lease_token = gen_random_uuid()")  # taken again
    assert not await alive(conn, seen["run"])


async def test_a_command_the_database_was_lost_under_fails_once_with_its_item_ended(
    conn: Session, db
):
    """Not tried again in process: the owner runs it again."""
    tries = []

    async def work() -> int:
        tries.append(1)
        raise psycopg.OperationalError("the server closed the connection unexpectedly")

    with pytest.raises(CommandCutOffError, match="run it again"):
        await inline(db, "intake", work)
    (item,) = await commands(conn)
    assert tries == [1]
    assert (item["state"], item["outcome"], item["attempts"]) == ("ended", "failed", 1)


async def test_a_command_that_loses_a_conflict_taking_its_claim_fails_without_working(
    conn: Session, db, monkeypatch
):
    """Taking its one claim loses a conflict: not tried again, the work never runs, and no
    item is left."""
    worked = []

    async def admit(*_, **__):
        raise psycopg.errors.SerializationFailure("could not serialize access")

    async def work() -> None:
        worked.append(1)

    monkeypatch.setattr(queue, "admit", admit)
    with pytest.raises(CommandCutOffError, match="run it again"):
        await asyncio.wait_for(inline(db, "intake", work), 10)
    assert worked == [] and await commands(conn) == []


async def test_a_command_waits_for_its_admission_then_runs_once(conn: Session, db):
    """Its admission held by a running item (`serve`'s Deep pass), it waits, with no item of its
    own meanwhile (a process killed as it waits leaves none), then runs."""
    async with db.write("a Deep pass, running") as unit:
        await queue.submit(unit, fulltext_store.DEEP_WORK, "1")
    await conn.execute(
        "UPDATE work_items SET state = 'running', lease_token = gen_random_uuid(), "
        "lease_until = now() + interval '1 hour'"
    )
    ran = []

    async def work() -> str:
        ran.append(1)
        return "its result"

    task = asyncio.create_task(inline(db, "fulltext --deep", work, admission="deep"))
    await asyncio.sleep(1.5)
    assert ran == [] and not task.done() and await commands(conn) == []
    await conn.execute(
        "UPDATE work_items SET state = 'ended', outcome = 'done', lease_token = NULL, "
        "lease_until = NULL, ended_at = now() WHERE kind <> 'command'"
    )
    assert await asyncio.wait_for(task, 10) == "its result"
    (item,) = await commands(conn)
    assert (item["outcome"], item["attempts"]) == ("done", 1)


async def test_a_command_whose_work_stops_cancelled_runs_once_and_fails(conn: Session, db):
    """Its work ends with `CancelledError` (not the command's): never run again, the item ended
    failed after its one attempt."""
    tries = []

    async def work() -> None:
        tries.append(1)
        raise asyncio.CancelledError

    with pytest.raises(CommandCutOffError, match="cancelled"):
        await inline(db, "intake", work)
    (item,) = await commands(conn)
    assert tries == [1] and (item["state"], item["outcome"], item["attempts"]) == (
        "ended",
        "failed",
        1,
    )


async def test_a_command_whose_heartbeat_fails_tells_its_waiter(conn: Session, db, monkeypatch):
    """Its lease cannot be renewed: its work is stopped and the command fails, cut off, its
    item ended."""
    from litwatch.gates import work as work_gate  # noqa: PLC0415

    monkeypatch.setattr(work_gate, "LEASE", timedelta(seconds=0.6))

    async def lost(self, item) -> bool:
        return False

    monkeypatch.setattr(WorkGate, "renew", lost)

    async def work() -> None:
        await asyncio.sleep(30)

    with pytest.raises(CommandCutOffError, match="lease was lost"):
        await asyncio.wait_for(inline(db, "intake", work), 10)
    (item,) = await commands(conn)
    assert (item["state"], item["outcome"]) == ("ended", "failed")


async def test_a_command_after_one_whose_process_died_takes_its_admission_back(conn: Session, db):
    """A command killed with its claim left its item running under `deep`, its lease since
    lapsed, and no `serve` runs: the next command for `deep` ends it failed and runs."""
    await conn.execute(
        "INSERT INTO work_items (kind, key, admission, state, lease_token, lease_until) "
        "VALUES ('command', 'fulltext --deep:gone', 'deep', 'running', gen_random_uuid(), "
        "now() - interval '1 second')"
    )

    async def work() -> str:
        return "its result"

    assert await asyncio.wait_for(inline(db, "fulltext --deep", work, admission="deep"), 10) == (
        "its result"
    )
    gone, ran = await commands(conn)
    assert (gone["outcome"], gone["error"]) == ("failed", "its process stopped")
    assert (ran["outcome"], ran["attempts"]) == ("done", 1)


@pytest.mark.parametrize("stops", ["Ctrl-C", "an error of the gate's"])
async def test_a_command_stopped_before_its_claim_leaves_no_item_pending(
    conn: Session, db, monkeypatch, stops
):
    """Cancelled as it takes its claim, or its claim raising: no item of its own is left, ended
    or not."""
    taking = asyncio.Event()
    real = queue.admit

    async def admit(unit, *args, **kwargs):
        await real(unit, *args, **kwargs)  # made in the unit, which then fails
        taking.set()
        if stops == "Ctrl-C":
            await asyncio.Event().wait()
        raise RuntimeError("the gate broke")

    async def work() -> None:
        raise AssertionError("it never runs")

    monkeypatch.setattr(queue, "admit", admit)
    task = asyncio.create_task(inline(db, "intake", work))
    await asyncio.wait_for(taking.wait(), 10)
    if stops == "Ctrl-C":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="the gate broke"):
            await task
    assert await commands(conn) == []


async def test_a_command_stopped_by_ctrl_c_ends_its_item(conn: Session, db):
    """Cancelled as it works (Ctrl-C), its item is ended failed, not left pending."""
    working = asyncio.Event()

    async def work() -> None:
        working.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(inline(db, "intake", work))
    await asyncio.wait_for(working.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (item,) = await commands(conn)
    assert (item["state"], item["outcome"]) == ("ended", "failed")
    assert item["error"] == "the command stopped before its item ended"


async def test_a_command_whose_end_was_not_recorded_does_not_succeed(
    conn: Session, db, monkeypatch
):
    """Its work returned, but recording its item done failed: the command fails rather than
    report a success that is not on record, and its item is ended failed."""
    real = queue.end

    async def end(unit, item, outcome, **kwargs):
        if item.kind == queue.COMMAND and outcome == Outcome.DONE:
            raise RuntimeError("the end is not recorded")
        return await real(unit, item, outcome, **kwargs)

    async def work() -> str:
        return "its result"

    monkeypatch.setattr(queue, "end", end)
    with pytest.raises(RuntimeError, match="its end was not recorded"):
        await inline(db, "intake", work)
    (item,) = await commands(conn)
    assert (item["state"], item["outcome"]) == ("ended", "failed")


async def test_a_command_that_fails_fails_its_item_and_raises(conn: Session, db):
    async def work() -> None:
        raise ValueError("no such category")

    with pytest.raises(ValueError, match="no such category"):
        await inline(db, "intake", work)
    (item,) = await commands(conn)
    assert item["outcome"] == "failed" and "no such category" in item["error"]


async def test_the_cli_deep_passes_wait_for_serves_and_close_their_clients_once(
    conn: Session, db, tmp_path, monkeypatch
):
    """`serve` works a Deep pass; `litwatch fulltext --deep`, through its real command, waits
    for it to end, then works, never beside it. It loses the database: it fails at once, not
    tried again, and its clients close once."""
    from collections import Counter  # noqa: PLC0415

    from litwatch import fulltext, integrations  # noqa: PLC0415
    from litwatch.fulltext import deep as deep_passes  # noqa: PLC0415
    from litwatch.integrations import deep as deep_integration  # noqa: PLC0415

    class Client:
        def __init__(self) -> None:
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    archive, maker = Client(), Client()
    deep, release, order = asyncio.Event(), asyncio.Event(), []

    async def pass_(_) -> None:
        deep.set()
        await release.wait()
        order.append("serve's pass")

    async def cli_passes(_, given_archive, given_maker, **kwargs) -> Counter[str]:
        assert (given_archive, given_maker) == (archive, maker)
        assert (archive.closed, maker.closed) == (0, 0)
        order.append("the CLI's")
        raise psycopg.OperationalError("the server closed the connection unexpectedly")

    monkeypatch.setattr(fulltext, "archive_for", lambda cfg: archive)
    monkeypatch.setattr(integrations, "enabled_using", lambda cfg, name: {"use": name})
    monkeypatch.setattr(deep_integration, "load", lambda name, options, cfg: maker)
    monkeypatch.setattr(deep_passes, "work", cli_passes)
    async with db.write("a Deep pass") as unit:
        await queue.submit(unit, fulltext_store.DEEP_WORK, "1")
    real_admit, refused = queue.admit, asyncio.Event()

    async def admit(unit, key, admission, lease):
        if (found := await real_admit(unit, key, admission, lease)) is None:
            assert admission == "deep"
            refused.set()
        return found

    monkeypatch.setattr(queue, "admit", admit)
    async with serving(WorkGate(db), {fulltext_store.DEEP_WORK: pass_}):
        await asyncio.wait_for(deep.wait(), 10)
        cli = asyncio.create_task(command(conn, tmp_path, monkeypatch, "fulltext", "--deep"))
        await asyncio.wait_for(refused.wait(), 10)  # it waits for `deep`, with no item yet
        assert await commands(conn) == [] and order == []
        release.set()
        result = await asyncio.wait_for(cli, 20)
    assert result.exit_code != 0 and "run it again" in result.output, result.output
    assert order == ["serve's pass", "the CLI's"]
    assert (archive.closed, maker.closed) == (1, 1)
    (item,) = await commands(conn)
    assert (item["attempts"], item["outcome"]) == (1, "failed")


async def test_the_cli_deep_passes_that_lose_their_admission_close_their_clients_once(
    conn: Session, tmp_path, monkeypatch
):
    """`litwatch fulltext --deep` loses the database as it takes its admission: it fails, cut
    off, with no work done, and the clients it opened close once all the same."""
    from litwatch import fulltext, integrations  # noqa: PLC0415
    from litwatch.integrations import deep as deep_integration  # noqa: PLC0415

    class Client:
        def __init__(self) -> None:
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    archive, maker = Client(), Client()

    async def admit(*_, **__):
        raise psycopg.OperationalError("the server closed the connection unexpectedly")

    monkeypatch.setattr(fulltext, "archive_for", lambda cfg: archive)
    monkeypatch.setattr(integrations, "enabled_using", lambda cfg, name: {"use": name})
    monkeypatch.setattr(deep_integration, "load", lambda name, options, cfg: maker)
    monkeypatch.setattr(queue, "admit", admit)
    result = await command(conn, tmp_path, monkeypatch, "fulltext", "--deep")
    assert result.exit_code != 0 and "run it again" in result.output, result.output
    assert (archive.closed, maker.closed) == (1, 1)
    assert await commands(conn) == []


async def test_a_command_returns_only_once_a_renewal_cut_off_has_unwound(
    conn: Session, db, monkeypatch
):
    """Its renewal does not answer before the lease lapses: the command fails, cut off, but not
    before that renewal, cancelled, has unwound, which uses the pool the command then closes."""
    from litwatch.gates import work as work_gate  # noqa: PLC0415

    monkeypatch.setattr(work_gate, "LEASE", timedelta(seconds=0.6))
    unwound = []

    async def stalls(self, item) -> bool:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)  # its unit rolling back
            unwound.append(True)
            raise
        return True

    monkeypatch.setattr(WorkGate, "renew", stalls)

    async def work() -> None:
        await asyncio.sleep(30)

    with pytest.raises(CommandCutOffError, match="lease was lost"):
        await asyncio.wait_for(inline(db, "intake", work), 10)
    assert unwound == [True]


@pytest.mark.parametrize("admission", ["deep", "intake:gone"])
async def test_a_command_whose_process_died_is_ended_by_any_take_back(conn: Session, db, admission):
    """Its item holds its admission (`deep`, or its own) until its lease lapses; then any
    `serve`'s take-back ends it failed (no process serves it again), and the Deep passes go
    on."""
    await conn.execute(
        "INSERT INTO work_items (kind, key, admission, state, lease_token, lease_until) "
        "VALUES ('command', 'intake:gone', %s, 'running', gen_random_uuid(), "
        "now() - interval '1 second')",
        (admission,),
    )
    async with db.write("a Deep pass") as unit:
        waiting = await queue.submit(unit, fulltext_store.DEEP_WORK, "1")
    gate = WorkGate(db)
    assert await gate.take_back([fulltext_store.DEEP_WORK]) == []
    (gone,) = await commands(conn)
    assert (gone["state"], gone["outcome"], gone["error"]) == (
        ItemState.ENDED,
        Outcome.FAILED,
        "its process stopped",
    )
    taken = await gate._take([fulltext_store.DEEP_WORK])
    assert taken is not None and taken[0].id == waiting.id  # the admission is free again


async def test_the_cli_nightly_runs_as_a_command_item(conn: Session, tmp_path, monkeypatch):
    """`litwatch nightly` through its real command: its run is the command item's, alive by its
    lease, and the item ends done."""
    from litwatch.jobs import nightly  # noqa: PLC0415
    from tests.scripted import Scripted  # noqa: PLC0415

    real, seen = nightly.run, {}

    async def scripted(db, cfg, **kwargs):  # the real run, on scripted models
        seen["work"] = current_work()
        return await real(db, cfg, gateway=Scripted().gateway, **kwargs)

    monkeypatch.setattr(nightly, "run", scripted)
    result = await command(conn, tmp_path, monkeypatch, "nightly", "--no-fetch", "--no-digests")
    assert result.exit_code == 0, result.output
    (item,) = await commands(conn)
    assert item["key"].startswith("nightly:") and item["outcome"] == "done"
    assert seen["work"].item.id == item["id"]
    (run,) = await (await conn.execute("SELECT * FROM runs WHERE kind = 'nightly'")).fetchall()
    assert run["work_id"] == item["id"] and run["finished_at"] is not None


async def test_a_cli_nightly_whose_close_fails_says_to_run_it_again(
    conn: Session, tmp_path, monkeypatch
):
    """`litwatch nightly` whose close (with its jobs' release) fails ends its item failed and
    tells the owner to run it again, rather than ending on an unhandled `UnclosedError`."""
    from litwatch.jobs import nightly  # noqa: PLC0415
    from litwatch.store import jobs as jobs_store  # noqa: PLC0415
    from tests.scripted import Scripted  # noqa: PLC0415

    real = nightly.run

    async def scripted(db, cfg, **kwargs):
        return await real(db, cfg, gateway=Scripted().gateway, **kwargs)

    async def lost(unit):
        raise psycopg.OperationalError("the server closed the connection unexpectedly")

    monkeypatch.setattr(nightly, "run", scripted)
    monkeypatch.setattr(jobs_store, "release", lost)
    result = await command(conn, tmp_path, monkeypatch, "nightly", "--no-fetch", "--no-digests")
    assert result.exit_code != 0 and "run it again" in result.output, result.output
    (item,) = await commands(conn)
    assert (item["attempts"], item["outcome"]) == (1, "failed")
