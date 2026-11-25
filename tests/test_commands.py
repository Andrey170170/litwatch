"""The CLI's commands as work items (docs/plans/a2-work-gate.md, P3): each runs its work as an
item of kind `command` in its own process (`gates.work.inline`), whose lease keeps its runs
alive; a lost database tries it again; one that does what a kind of `serve`'s does takes that
kind's admission, so the two run one at a time; and one whose process died is ended by the
`serve` that shares its admission. On real Postgres."""

import asyncio

import psycopg
import pytest

from litwatch.core.work import ItemState, Outcome
from litwatch.gates.context import current_work
from litwatch.gates.work import WorkGate, inline
from litwatch.store import fulltext as fulltext_store
from litwatch.store import runs
from litwatch.store import work as queue
from tests.sessions import Session
from tests.working import command, serving, until


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


async def test_a_command_the_database_was_lost_under_is_tried_again(conn: Session, db):
    tries = []

    async def work() -> int:
        tries.append(1)
        if len(tries) == 1:
            raise psycopg.OperationalError("the server closed the connection unexpectedly")
        return len(tries)

    assert await inline(db, "intake", work) == 2
    (item,) = await commands(conn)
    assert (item["attempts"], item["outcome"]) == (2, "done")


async def test_a_command_that_loses_the_database_reading_its_item_reads_it_again(
    conn: Session, db, monkeypatch
):
    """The command's own read of whether its item ended loses the database: it reads again,
    rather than leave its item running with no one waiting on it."""
    real, lost = queue.get, []

    async def get(unit, item_id):
        if not lost:
            lost.append(item_id)
            raise psycopg.OperationalError("the server closed the connection unexpectedly")
        return await real(unit, item_id)

    async def work() -> str:
        return "its result"

    monkeypatch.setattr(queue, "get", get)
    assert await inline(db, "intake", work) == "its result"
    (item,) = await commands(conn)
    assert lost == [item["id"]] and item["outcome"] == "done"


async def test_a_command_that_fails_fails_its_item_and_raises(conn: Session, db):
    async def work() -> None:
        raise ValueError("no such category")

    with pytest.raises(ValueError, match="no such category"):
        await inline(db, "intake", work)
    (item,) = await commands(conn)
    assert item["outcome"] == "failed" and "no such category" in item["error"]


async def test_the_cli_deep_passes_wait_for_serves_and_keep_their_clients_across_tries(
    conn: Session, db, tmp_path, monkeypatch
):
    """`serve` works a Deep pass; `litwatch fulltext --deep`, through its real command, waits
    for it to end, then works, never beside it. Its first try loses the database: the try made
    again works on the same clients, still open, which close once, at the end."""
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
        if order.count("the CLI's") == 1:
            raise psycopg.OperationalError("the server closed the connection unexpectedly")
        return Counter()

    monkeypatch.setattr(fulltext, "archive_for", lambda cfg: archive)
    monkeypatch.setattr(integrations, "enabled_using", lambda cfg, name: {"use": name})
    monkeypatch.setattr(deep_integration, "load", lambda name, options, cfg: maker)
    monkeypatch.setattr(deep_passes, "work", cli_passes)
    async with db.write("a Deep pass") as unit:
        await queue.submit(unit, fulltext_store.DEEP_WORK, "1")
    async with serving(WorkGate(db), {fulltext_store.DEEP_WORK: pass_}):
        await asyncio.wait_for(deep.wait(), 10)
        cli = asyncio.create_task(command(conn, tmp_path, monkeypatch, "fulltext", "--deep"))

        async def submitted() -> bool:
            return bool(await commands(conn))

        await until(submitted, "the CLI's item to be submitted")
        await asyncio.sleep(0.3)
        (waiting,) = await commands(conn)
        assert waiting["state"] == "pending" and waiting["admission"] == "deep" and order == []
        release.set()
        result = await asyncio.wait_for(cli, 20)
    assert result.exit_code == 0, result.output
    assert order == ["serve's pass", "the CLI's", "the CLI's"]
    assert (archive.closed, maker.closed) == (1, 1)
    (item,) = await commands(conn)
    assert (item["attempts"], item["outcome"]) == (2, "done")


async def test_a_command_whose_process_died_is_ended_by_the_serve_sharing_its_admission(
    conn: Session, db
):
    """Its item holds the `deep` admission until its lease lapses; then `serve`'s take-back
    ends it failed (no process serves it), and the Deep passes go on."""
    await conn.execute(
        "INSERT INTO work_items (kind, key, admission, state, lease_token, lease_until) "
        "VALUES ('command', 'fulltext --deep:gone', 'deep', 'running', gen_random_uuid(), "
        "now() - interval '1 second')"
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
