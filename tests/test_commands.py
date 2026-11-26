"""The CLI's commands as work items `serve` runs (docs/plans/a2-work-gate.md, P5; V6): a command
writes its item to the database `serve` works from and follows it, refused when no `serve`
runs its kind; it holds nothing of the item's, so `serve` may stop and start again under it;
Ctrl-C cancels the item or leaves it running, and `litwatch work follow` picks it back up; an
evaluation's answers come back as the item's results, into the command's file, and a run
stopped early resumes with what the file lacks. Through the real CLI and `serve`'s app, on
real Postgres, the models fake."""

import asyncio
import contextlib
import functools
import json
import threading
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from litwatch import cli
from litwatch.api import app as app_module
from litwatch.archive import ArchiveClient
from litwatch.config import Config, Models, RoleConfig
from litwatch.gates.work import WorkGate
from litwatch.integrations import archive as archive_integration
from litwatch.jobs import commands
from litwatch.jobs import intake as intake_job
from litwatch.llm import ModelGate, Reply, Stop, Usage
from litwatch.quality import classify, jsonl
from litwatch.store import papers as papers_store
from litwatch.store import work as queue
from tests.conftest import paper
from tests.fakes_http import FakeArchive, mock
from tests.scripted import CLASSIFY, Scripted, decision
from tests.sessions import Session, in_write
from tests.working import command, served, until

PING = Models(brief=RoleConfig(provider="anthropic", model="test-brief"))
LABELS = Path(__file__).parent.parent / "evals" / "labels"


async def rows(conn: Session, query: str, *params) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def items(conn: Session, name: str) -> list[dict]:
    return await rows(
        conn, "SELECT * FROM work_items WHERE kind = %s ORDER BY id", f"command:{name}"
    )


class Held:
    """A provider whose calls wait until `release` is set, answering 17 + 25; `started` (across
    threads: the command runs in one) set as the first call waits, `cancelled` its calls cut
    off. The first `free` calls answer at once."""

    name = "anthropic"

    def __init__(self, answer: str = '{"total": 42}', free: int = 0) -> None:
        self.answer, self.free = answer, free
        self.started, self.release = threading.Event(), asyncio.Event()
        self.asked: list[str] = []
        self.cancelled = 0

    def routing(self, role):
        return role.routing

    async def complete(self, role, request, **limits) -> Reply:
        self.asked.append(request.user)
        if len(self.asked) > self.free:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return Reply(self.answer, Stop.COMPLETE, Usage(model=role.model, cost=0.001))


@pytest.fixture
def short_leases(monkeypatch):
    """`serve`'s Work gate on leases of 0.6 s, renewed every 0.2 s: it sees a lease taken away
    (a cancel) at once."""
    monkeypatch.setattr(
        app_module, "WorkGate", functools.partial(WorkGate, lease=timedelta(seconds=0.6))
    )


def interrupted(monkeypatch, when: threading.Event) -> None:
    """The command's first follow is cut by Ctrl-C once `when` is set, following until then;
    a later one follows."""
    real, cut = cli._follow, []

    async def follow(db, item_id):
        if cut:
            return await real(db, item_id)
        cut.append(item_id)
        following = asyncio.create_task(real(db, item_id))
        while not when.is_set():
            if following.done():
                return following.result()
            await asyncio.sleep(0.02)
        following.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await following
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_follow", follow)


@pytest.mark.parametrize(
    "argv",
    [("nightly", "--limit", "5"), ("ping", "brief"), ("eval", "replay", "--resume", "1", "--yes")],
)
async def test_a_command_with_no_serve_is_refused_and_writes_nothing(
    conn: Session, tmp_path, monkeypatch, argv
):
    if argv[0] == "eval":  # a replay is prepared here first, with work left, to start
        from litwatch.quality import replay  # noqa: PLC0415

        async def resume(unit, cfg, eval_run_id):
            return _Replay()

        async def estimate(unit, cfg, r):
            return {"classification": 0.0, "grading": 0.0}

        monkeypatch.setattr(replay, "resume", resume)
        monkeypatch.setattr(replay, "estimate", estimate)
    result = await command(conn, tmp_path, monkeypatch, *argv)
    assert result.exit_code != 0, result.output
    assert "no `litwatch serve` runs" in result.output and "dev-host.md" in result.output
    assert await rows(conn, "SELECT * FROM work_items") == []
    assert await rows(conn, "SELECT * FROM eval_runs") == []  # a replay starts with its item


class _Replay:
    """A replay prepared, with work left and no record yet, as the CLI's preparation gives it."""

    eval_run_id = None
    left_out = stale = labels = papers = ()
    to_classify, to_grade = ["2610.00001"], []


async def test_a_nightly_follows_its_item_across_a_serve_restart_and_is_done_once(
    conn: Session, db, tmp_path, monkeypatch
):
    """`litwatch nightly --limit 5` writes its item and waits; `serve` runs it, and stops while
    it runs (its claim given back); the command says no `serve` runs it, holds nothing, and
    follows the same item when `serve` starts again and runs it to its end: one item, one run,
    its record printed once."""
    real, attempts = commands.nightly_job.run, []
    running = threading.Event()

    async def nightly(db_, cfg, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            running.set()
            await asyncio.Event().wait()  # until `serve` stops
        return await real(db_, cfg, **kwargs)

    monkeypatch.setattr(commands.nightly_job, "run", nightly)
    models = Scripted()
    config = Config()
    async with served(db, config, models=models.gate(config)):
        cli_task = asyncio.create_task(
            command(conn, tmp_path, monkeypatch, "nightly", "--no-fetch", "--limit", "5")
        )

        async def runs() -> bool:
            return running.is_set()

        await until(runs, "serve to run the nightly")
    (item,) = await items(conn, "nightly")
    assert item["state"] == "pending" and not cli_task.done()  # given back; the command waits
    await asyncio.sleep(2.5)  # it looks again, finds no `serve`
    async with served(db, config, models=models.gate(config)):
        result = await asyncio.wait_for(cli_task, 20)
    assert result.exit_code == 0, result.output
    assert f"item {item['id']}: command:nightly" in result.output
    assert "no `litwatch serve` runs command:nightly now" in result.output
    (item,) = await items(conn, "nightly")
    assert (item["state"], item["outcome"], item["attempts"]) == ("ended", "done", 2)
    assert [a["limit"] for a in attempts] == [5, 5]
    (run,) = await rows(conn, "SELECT * FROM runs WHERE kind = 'nightly'")
    assert run["work_id"] == item["id"] and run["finished_at"] is not None
    assert result.output.count(f"run {run['id']}: cost") == 1


@pytest.mark.usefixtures("short_leases")
async def test_ctrl_c_left_running_is_followed_again_and_cancelled_stops_serves_call(
    conn: Session, db, tmp_path, monkeypatch
):
    """Ctrl-C as `serve` runs the ping: left running, the command exits and says how to follow
    it; `litwatch work follow` shows its answer. A second ping, cancelled: its item ends failed
    and `serve` stops its call."""
    held = Held()
    async with served(db, Config(), models=ModelGate(PING, {"anthropic": held})):
        interrupted(monkeypatch, held.started)
        left = await command(conn, tmp_path, monkeypatch, "ping", "brief", input="n\n")
        assert left.exit_code == 130, left.output
        (item,) = await items(conn, "ping")
        assert item["state"] == "running"
        assert f"litwatch work follow {item['id']}" in left.output
        following = asyncio.create_task(
            command(conn, tmp_path, monkeypatch, "work", "follow", str(item["id"]))
        )
        await asyncio.sleep(0.3)
        held.release.set()
        followed = await asyncio.wait_for(following, 20)
        assert followed.exit_code == 0, followed.output
        assert "test-brief -> 42" in followed.output

        held.release.clear()
        held.started.clear()
        interrupted(monkeypatch, held.started)
        cancelled = await command(conn, tmp_path, monkeypatch, "ping", "brief", input="y\n")
        assert cancelled.exit_code != 0 and "ended failed" in cancelled.output, cancelled.output

        async def stopped() -> bool:
            return held.cancelled == 1

        await until(stopped, "serve to stop the cancelled item's call")
    first, second = await items(conn, "ping")
    assert (first["outcome"], second["outcome"]) == ("done", "failed")
    assert second["error"] == "cancelled by the owner"
    # The ping's answer is its completion record, kept in the unit that ended it: no result.
    (said,) = first["result"]
    assert "test-brief -> 42" in said["say"]
    assert await rows(conn, "SELECT * FROM work_results WHERE work_id = %s", first["id"]) == []


@pytest.mark.usefixtures("short_leases")
async def test_an_evaluation_stopped_early_resumes_with_what_its_file_lacks(
    conn: Session, db, tmp_path, monkeypatch
):
    """`litwatch eval classify` on 10 labelled papers: `serve` answers 4, then the command is
    cancelled; run again, it submits the 6 its file lacks, and the file holds each paper once.
    Its calls are an evaluation run's."""
    ids = classify.load(LABELS).papers[:10]
    await in_write(conn, papers_store.upsert, [paper(i, title=f"Paper {i}") for i in ids])
    held = Held(json.dumps(decision()), free=4)
    held.name = "openrouter"
    (tmp_path / "candidates.toml").write_text(
        f'[decider]\nprovider = "openrouter"\nmodel = "{CLASSIFY.model}"\n'
    )
    out = tmp_path / "runs"
    argv = ("eval", "classify", "decider", "--candidates", str(tmp_path / "candidates.toml"),
            "--labels", str(LABELS), "--out", str(out), "--limit", "10")  # fmt: skip
    path = out / "decider.jsonl"

    def answered() -> list[str]:
        return [json.loads(x)["paper_id"] for x in jsonl.lines(path)]

    async with served(db, Config(), models=ModelGate(Models(), {"openrouter": held})):
        four = threading.Event()

        async def written() -> bool:
            if len(answered()) >= 4:
                four.set()
            return four.is_set()

        interrupted(monkeypatch, four)
        watching = asyncio.create_task(until(written, "4 answers in the file"))
        stopped = await command(conn, tmp_path, monkeypatch, *argv, input="y\n")
        await watching
        assert stopped.exit_code != 0, stopped.output
        first = answered()
        assert len(first) == 4
        with path.open("ab") as f:  # a kill cut a record off mid-write, mid-character
            f.write('{"paper_id": "é'.encode()[:-1])
        held.release.set()
        resumed = await command(conn, tmp_path, monkeypatch, *argv)
    assert resumed.exit_code == 0, resumed.output
    assert "decider: 6 sorted" in resumed.output  # sorted or failed: each asked once
    assert sorted(answered()) == sorted(ids) and len(answered()) == 10
    second = (await items(conn, "classify"))[1]
    (candidate,) = second["scope"]["candidates"]
    assert set(candidate["papers"]) == set(ids) - set(first)  # only what the file lacked
    kinds = await rows(
        conn, "SELECT DISTINCT r.kind FROM inference_calls c JOIN runs r ON r.id = c.run_id"
    )
    assert [k["kind"] for k in kinds] == ["eval"]


async def test_intake_archive_here_runs_in_the_command_for_a_database_no_serve_works_from(
    conn: Session, tmp_path, monkeypatch
):
    """`litwatch intake archive --here`, with no `serve`: it runs in the command itself, no
    work item and no Work gate (so nothing says it serves a command's kind), no Model gate."""
    seen = []

    class Done:
        fetched = inserted = 3
        updated = 0
        details = {"skipped": []}

    async def from_archive(db, cfg, client, first, last):
        seen.append((first.isoformat(), last.isoformat()))
        return Done()

    async def aclose():
        pass

    monkeypatch.setattr(intake_job, "from_archive", from_archive)
    monkeypatch.setattr(
        archive_integration, "source", lambda cfg: type("C", (), {"aclose": staticmethod(aclose)})()
    )
    argv = ("intake", "archive", "--since", "2026-10-01", "--until", "2026-10-02", "--here")
    result = await command(conn, tmp_path, monkeypatch, *argv, config=None)
    assert result.exit_code == 0, result.output
    assert "intake from the archive, 2026-10-01 to 2026-10-02: 3 papers, 3 new" in result.output
    assert seen == [("2026-10-01", "2026-10-02")]
    assert await rows(conn, "SELECT * FROM work_items") == []
    assert await rows(conn, "SELECT * FROM workers") == []


async def test_a_nightly_whose_run_closed_is_not_run_again_when_serve_restarts(
    conn: Session, db, tmp_path, monkeypatch
):
    """The nightly's run closed, then `serve` stopped before its item ended (given back): the
    next attempt finds the run its item closed and returns that run's record. One run."""
    real, attempts = commands.nightly_job.run, []
    closed = threading.Event()

    async def nightly(db_, cfg, **kwargs):
        attempts.append(kwargs)
        run_id = await real(db_, cfg, **kwargs)
        closed.set()
        await asyncio.Event().wait()  # until `serve` stops, its run closed
        return run_id

    monkeypatch.setattr(commands.nightly_job, "run", nightly)
    models, config = Scripted(), Config()
    async with served(db, config, models=models.gate(config)):
        cli_task = asyncio.create_task(
            command(conn, tmp_path, monkeypatch, "nightly", "--no-fetch", "--limit", "5")
        )

        async def ran() -> bool:
            return closed.is_set()

        await until(ran, "serve to close the nightly's run")
    async with served(db, config, models=models.gate(config)):
        result = await asyncio.wait_for(cli_task, 20)
    assert result.exit_code == 0, result.output
    (item,) = await items(conn, "nightly")
    assert (item["outcome"], item["attempts"]) == ("done", 2)
    (run,) = await rows(conn, "SELECT * FROM runs WHERE kind = 'nightly'")
    assert len(attempts) == 1  # not run again
    assert f"run {run['id']} closed in an earlier attempt" in result.output
    assert result.output.count(f"run {run['id']}: cost") == 1


async def test_a_fulltext_limit_counts_what_its_earlier_attempts_worked(
    conn: Session, db, tmp_path, monkeypatch
):
    """`litwatch fulltext --limit 5`: its first attempt works 2 passes, counted in the unit
    that records them, and `serve` stops; the next attempt works at most the 3 left."""
    limits: list[int | None] = []
    stopped = threading.Event()

    async def work_waiting(db_, config, archive, *, limit, wait, models, report, counted):
        limits.append(limit)
        if len(limits) == 1:
            async with db_.write("test: two passes recorded") as unit:
                await counted(unit, 2)
            stopped.set()
            await asyncio.Event().wait()
        return Counter(briefs_written=limit)

    monkeypatch.setattr(commands.fulltext, "work_waiting", work_waiting)
    archive = ArchiveClient("http://archive.test", "t", http=mock(FakeArchive().handle))
    async with served(db, Config(), archive=archive):
        cli_task = asyncio.create_task(
            command(conn, tmp_path, monkeypatch, "fulltext", "--limit", "5")
        )

        async def worked() -> bool:
            return stopped.is_set()

        await until(worked, "serve to work two passes")
    async with served(db, Config(), archive=archive):
        result = await asyncio.wait_for(cli_task, 20)
    assert result.exit_code == 0, result.output
    assert limits == [5, 3]
    (item,) = await items(conn, "fulltext")
    assert item["progress"] == {"worked": 2}


async def _classify_left_running(conn: Session, db, tmp_path, monkeypatch, held: Held):
    """`eval classify` of 10 labelled papers, `serve` answering 4 before it waits on `held`,
    left running at Ctrl-C; the command's arguments, its file, and what reads it."""
    ids = classify.load(LABELS).papers[:10]
    await in_write(conn, papers_store.upsert, [paper(i, title=f"Paper {i}") for i in ids])
    (tmp_path / "candidates.toml").write_text(
        f'[decider]\nprovider = "openrouter"\nmodel = "{CLASSIFY.model}"\n'
    )
    argv = ("eval", "classify", "decider", "--candidates", str(tmp_path / "candidates.toml"),
            "--labels", str(LABELS), "--out", str(tmp_path / "runs"), "--limit", "10")  # fmt: skip
    path = tmp_path / "runs" / "decider.jsonl"
    four = threading.Event()

    async def written() -> bool:
        if len(jsonl.lines(path)) >= 4:
            four.set()
        return four.is_set()

    interrupted(monkeypatch, four)
    watching = asyncio.create_task(until(written, "4 answers in the file"))
    left = await command(conn, tmp_path, monkeypatch, *argv, input="n\n")
    await watching
    assert left.exit_code == 130, left.output
    return ids, argv, path


@pytest.mark.usefixtures("short_leases")
async def test_an_evaluation_rerun_writes_in_what_its_item_answered_while_no_one_followed(
    conn: Session, db, tmp_path, monkeypatch
):
    """Ctrl-C leaves `eval classify` running and `serve` answers the rest with no command
    following it: its answers are only the item's results. Run again, the command writes
    them into its file before it decides what is left, so it asks nothing again."""
    held = Held(json.dumps(decision()), free=4)
    held.name = "openrouter"
    async with served(db, Config(), models=ModelGate(Models(), {"openrouter": held})):
        ids, argv, path = await _classify_left_running(conn, db, tmp_path, monkeypatch, held)
        held.release.set()

        async def ended() -> bool:
            return (await items(conn, "classify"))[0]["state"] == "ended"

        await until(ended, "the item to end, no one following it")
        assert len(jsonl.lines(path)) == 4
        again = await command(conn, tmp_path, monkeypatch, *argv)
    assert again.exit_code == 0, again.output
    assert "nothing left to ask" in again.output
    assert sorted(json.loads(x)["paper_id"] for x in jsonl.lines(path)) == sorted(ids)
    assert len(held.asked) == 10 and len(await items(conn, "classify")) == 1


@pytest.mark.usefixtures("short_leases")
async def test_an_evaluation_rerun_follows_its_item_while_it_still_runs(
    conn: Session, db, tmp_path, monkeypatch
):
    """Run again while the item left running still writes into its file, the command follows
    that item to its end instead of submitting another."""
    held = Held(json.dumps(decision()), free=4)
    held.name = "openrouter"
    async with served(db, Config(), models=ModelGate(Models(), {"openrouter": held})):
        ids, argv, path = await _classify_left_running(conn, db, tmp_path, monkeypatch, held)
        again = asyncio.create_task(command(conn, tmp_path, monkeypatch, *argv))
        await asyncio.sleep(0.5)
        held.release.set()
        result = await asyncio.wait_for(again, 20)
    assert result.exit_code == 0, result.output
    (item,) = await items(conn, "classify")
    assert f"item {item['id']} still writes into these outputs" in result.output
    assert sorted(json.loads(x)["paper_id"] for x in jsonl.lines(path)) == sorted(ids)
    assert len(held.asked) == 10


async def test_work_follow_is_for_a_commands_item_only(conn: Session, db, tmp_path, monkeypatch):
    other = await in_write(conn, queue.submit, "fulltext", "1")
    result = await command(conn, tmp_path, monkeypatch, "work", "follow", str(other.id))
    assert result.exit_code != 0 and f"no command's work item {other.id}" in result.output


def test_serve_runs_each_intake_where_it_has_what_the_handler_needs():
    """arXiv intake whatever `serve` has (the schedule off too); a window from the archive only
    with an archive; the catalog pass only when it harvests."""

    def kinds(config: Config, archive: object = None) -> set[str]:
        serving = commands.Serving(cast(Any, None), config, cast(Any, None), cast(Any, archive))
        return {k for k in commands.CommandWorker(serving).kinds() if "intake" in k}

    assert kinds(Config()) == {"command:intake"}
    assert kinds(Config(), archive=object()) == {"command:intake", "command:intake-archive"}
    harvests = Config(integrations={"archive": {"harvest": True}})
    assert kinds(harvests) == {"command:intake", "command:intake-catalog"}
