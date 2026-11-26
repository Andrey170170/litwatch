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
from datetime import timedelta
from pathlib import Path

import pytest

from litwatch import cli
from litwatch.api import app as app_module
from litwatch.config import Config, Models, RoleConfig
from litwatch.gates.work import WorkGate
from litwatch.jobs import commands
from litwatch.llm import ModelGate, Reply, Stop, Usage
from litwatch.quality import classify
from litwatch.store import papers as papers_store
from tests.conftest import paper
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
    if argv[0] == "eval":  # a replay is prepared here first: as if one were found to resume
        prepared = (_Replay(), {"classification": 0.0, "grading": 0.0})
        monkeypatch.setattr(cli, "_run", lambda work, **_: prepared)
    result = await command(conn, tmp_path, monkeypatch, *argv)
    assert result.exit_code != 0, result.output
    assert "no `litwatch serve` runs" in result.output and "dev-host.md" in result.output
    assert await rows(conn, "SELECT * FROM work_items") == []


class _Replay:
    """A replay found to resume, with work left, as the CLI's preparation gives it."""

    eval_run_id = 7
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
    assert f"item {item['id']} runs again (attempt 2)" in result.output
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
    assert second["error"] == "cancelled by the owner (Ctrl-C)"


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
        return (
            [json.loads(x)["paper_id"] for x in path.read_text().splitlines()]
            if path.exists()
            else []
        )

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
        held.release.set()
        resumed = await command(conn, tmp_path, monkeypatch, *argv)
    assert resumed.exit_code == 0, resumed.output
    assert "decider: 6 sorted" in resumed.output  # sorted or failed: each asked once
    assert sorted(answered()) == sorted(ids) and len(answered()) == 10
    second = (await items(conn, "classify"))[1]
    assert set(json.loads(json.dumps(second["scope"]))["candidates"][0]["papers"]) == set(
        ids
    ) - set(first)
    kinds = await rows(
        conn, "SELECT DISTINCT r.kind FROM inference_calls c JOIN runs r ON r.id = c.run_id"
    )
    assert [k["kind"] for k in kinds] == ["eval"]


async def test_intake_archive_here_runs_in_the_command_for_a_database_no_serve_works_from(
    conn: Session, tmp_path, monkeypatch
):
    """`litwatch intake archive --here`, with no `serve`: its item runs in the command's own
    Work gate, restricted to it, and ends done."""
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

    monkeypatch.setattr(commands.intake_job, "from_archive", from_archive)
    monkeypatch.setattr(
        commands.archive_integration,
        "source",
        lambda cfg: type("C", (), {"aclose": staticmethod(aclose)})(),
    )
    argv = ("intake", "archive", "--since", "2026-10-01", "--until", "2026-10-02", "--here")
    result = await command(conn, tmp_path, monkeypatch, *argv, config=None)
    assert result.exit_code == 0, result.output
    assert "intake from the archive, 2026-10-01 to 2026-10-02: 3 papers, 3 new" in result.output
    assert seen == [("2026-10-01", "2026-10-02")]
    (item,) = await items(conn, "intake")
    assert item["outcome"] == "done"
    assert await rows(conn, "SELECT * FROM workers") == []  # its gate retired
