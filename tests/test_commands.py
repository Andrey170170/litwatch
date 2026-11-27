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
import io
import json
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from rich.console import Console
from typer.testing import CliRunner

from litwatch import cli
from litwatch.api import app as app_module
from litwatch.config import Config, Models, RoleConfig
from litwatch.core.work import ItemState, Outcome, WorkItem
from litwatch.gates.work import WorkGate
from litwatch.integrations import archive as archive_integration
from litwatch.jobs import commands
from litwatch.jobs import intake as intake_job
from litwatch.llm import ModelGate, Reply, Stop, Usage
from litwatch.quality import classify, jsonl
from litwatch.store import papers as papers_store
from litwatch.store import work as queue
from tests import settling
from tests.conftest import TREE, paper
from tests.fakes import ScriptedProvider
from tests.scripted import CLASSIFY, CONFIG, ROLES, A, B, Scripted, decision
from tests.sessions import Session, in_write
from tests.working import CONFIG_TOML, command, served, until

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

    async def follow(db, item_id, followed=None):
        if cut:
            return await real(db, item_id, followed)
        cut.append(item_id)
        following = asyncio.create_task(real(db, item_id, followed))
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


class Pausing(ScriptedProvider):
    """The scripted provider whose first `analyse` call never answers: `paused` (across
    threads) set as it waits, until `serve` stops under it."""

    def __init__(self, name: str, respond) -> None:
        super().__init__(name, respond)
        self.paused = threading.Event()

    async def complete(self, role, request) -> Reply:
        if role.model == ROLES["analyse"].model and not self.paused.is_set():
            self.paused.set()
            await asyncio.Event().wait()
        return await super().complete(role, request)


async def test_a_nightly_survives_a_serve_restart_and_does_nothing_committed_twice(
    conn: Session, db, tmp_path, monkeypatch
):
    """V6: `litwatch nightly --no-fetch --limit 5` on two papers, through the real pipeline;
    `serve` stops once both classifications have committed, as the first analysis is asked.
    The item ends saying `serve` stopped; the command says so, waits for the next `serve`, and
    submits it again, which goes on from what committed: each paper classified once, its run
    closed, then a second run to its end."""
    await settling.save_tree(conn, TREE)
    await in_write(conn, papers_store.upsert, [A, B])
    models = Scripted()
    pausing = Pausing("anthropic", models.respond)
    providers = {"anthropic": pausing, "openrouter": ScriptedProvider("openrouter", models.respond)}

    def gate() -> ModelGate:
        return ModelGate(CONFIG.models, providers, retry_delays=(0.0,))

    async with served(db, CONFIG, models=gate()):
        cli_task = asyncio.create_task(
            command(conn, tmp_path, monkeypatch, "nightly", "--no-fetch", "--limit", "5")
        )

        async def classified() -> bool:
            n = await rows(conn, "SELECT count(*) AS n FROM classifications")
            return pausing.paused.is_set() and n[0]["n"] == 2

        await until(classified, "both classifications committed, the first analysis asked")
    first, *_ = await items(conn, "nightly")
    assert (first["state"], first["outcome"], first["error"]) == (
        "ended", "failed", queue.STOPPED
    )  # fmt: skip
    assert first["attempts"] == 1 and not cli_task.done()
    async with served(db, CONFIG, models=gate()):
        result = await asyncio.wait_for(cli_task, 30)
    assert result.exit_code == 0, result.output
    first, second = await items(conn, "nightly")
    assert (second["outcome"], second["attempts"], second["scope"]) == (
        "done", 1, first["scope"]
    )  # fmt: skip
    assert f"item {first['id']}: serve stopped while it ran; submitted again" in result.output
    assert f"item {second['id']}: command:nightly" in result.output
    classify = Counter(title for step, title in models.asked if step == "classify")
    assert classify == {A.title: 1, B.title: 1}  # nothing committed done twice
    found = await rows(conn, "SELECT * FROM runs WHERE kind = 'nightly' ORDER BY id")
    assert [r["work_id"] for r in found] == [first["id"], second["id"]]
    assert all(r["finished_at"] is not None for r in found)  # the first closed as it stopped
    assert result.output.count(f"run {found[1]['id']}: cost") == 1


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


async def test_ctrl_c_as_the_item_ends_shows_it_as_the_follower_does(
    conn: Session, db, tmp_path, monkeypatch
):
    """Ctrl-C lands as the ping's item ends, before the follower showed its end: the command
    asks nothing, and shows the item as the follower does, its completion record."""
    held = Held(free=1)
    real, cut = cli._follow, []

    async def follow(db_, item_id, followed=None):
        if cut:
            return await real(db_, item_id, followed)
        cut.append(item_id)
        while True:
            async with db_.read("test: has it ended") as unit:
                found = await queue.get(unit, item_id)
            if found is not None and found.state == ItemState.ENDED:
                raise KeyboardInterrupt
            await asyncio.sleep(0.02)

    monkeypatch.setattr(cli, "_follow", follow)
    async with served(db, Config(), models=ModelGate(PING, {"anthropic": held})):
        result = await command(conn, tmp_path, monkeypatch, "ping", "brief")
    assert result.exit_code == 0, result.output
    assert "test-brief -> 42" in result.output and "Cancel item" not in result.output


async def test_a_replay_resumed_follows_its_open_item_before_it_estimates(
    conn: Session, db, tmp_path, monkeypatch
):
    """A replay has one item open, keyed by its run: `eval replay --resume` while it runs
    (detached) follows it to its end, and neither estimates nor submits a second writer."""
    from litwatch.quality import replay  # noqa: PLC0415

    release, resumed = asyncio.Event(), []

    async def handler(s, item, out):
        await out.say(f"replaying {item.scope['eval_run_id']}")
        await release.wait()
        return commands.finished()

    async def resume(*args, **kwargs):
        resumed.append(args)

    monkeypatch.setitem(cast(dict, commands.HANDLERS), "replay", (handler, lambda s: True))
    monkeypatch.setattr(replay, "resume", resume)
    async with served(db, Config()):
        detached = await in_write(
            conn, queue.submit, "command:replay", "41", scope={"eval_run_id": 41}
        )

        async def running() -> bool:
            return (await items(conn, "replay"))[0]["state"] == "running"

        await until(running, "serve to run the replay")
        again = asyncio.create_task(
            command(conn, tmp_path, monkeypatch, "eval", "replay", "--resume", "41", "--yes")
        )
        await asyncio.sleep(0.5)
        release.set()
        result = await asyncio.wait_for(again, 20)
    assert result.exit_code == 0, result.output
    assert f"item {detached.id} still runs replay 41: following it" in result.output
    assert "replaying 41" in result.output
    assert resumed == [] and len(await items(conn, "replay")) == 1


async def test_a_deep_limit_counts_the_jobs_ended_not_the_passes_awaiting_text(monkeypatch):
    """`fulltext --deep --limit 1 --wait 1`: a pass claimed while its text is still coming does
    not use the limit up, so the next look submits its Deep job; a job that goes on past the
    item, on that second look, is told to the command."""
    looks: list[int | None] = []
    found = [Counter(awaiting_text=1), Counter(cards_written=1)]

    async def work(db, archive, deep, *, rules, limit, fires, left):
        looks.append(limit)
        if len(looks) == 2:
            await left("2610.00001")  # as its cut-off job is let go
        return found[len(looks) - 1]

    class Out:
        said: list[str] = []

        async def data(self, data) -> None:
            pass

        async def say(self, text: str) -> None:
            self.said.append(text)

    monkeypatch.setattr(commands.deep_passes, "work", work)
    monkeypatch.setattr(commands.fulltext, "TICK_SECONDS", 0)
    s = commands.Serving(cast(Any, None), Config(), cast(Any, None), cast(Any, 1), cast(Any, 1))
    item = WorkItem(1, "command:deep", "k", "deep", ItemState.RUNNING, datetime.now(UTC), 1,
                    {"limit": 1, "wait": 1})  # fmt: skip
    out = Out()
    await commands._deep(s, item, cast(Any, out))
    assert looks == [1, 1]
    assert out.said == ["2610.00001: its Deep job goes on; the pass's next claim follows it"]


def test_the_name_of_an_outputs_item_is_replaced_whole(tmp_path, monkeypatch):
    """The sidecar naming an evaluation's item is written to a sibling and moved over it: a
    write cut off leaves the old name whole."""
    path = tmp_path / "decider.jsonl.item"
    cli._owned([path], "command:classify", "old")
    real = Path.write_text

    def torn(self, text, *args, **kwargs):
        real(self, text[:5], *args, **kwargs)
        raise OSError("killed mid-write")

    with monkeypatch.context() as m:
        m.setattr(Path, "write_text", torn)
        with pytest.raises(OSError, match="killed"):
            cli._owned([path], "command:classify", "new")
    assert json.loads(path.read_text())["key"] == "old"
    cli._owned([path], "command:classify", "new")
    assert json.loads(path.read_text())["key"] == "new"
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


@pytest.mark.usefixtures("short_leases")
async def test_an_evaluation_rerun_asking_more_follows_the_open_item_then_asks_the_rest(
    conn: Session, db, tmp_path, monkeypatch
):
    """Run again with --limit 12 while the item of --limit 10 still runs: the command follows
    that item to its end first, then submits what its own arguments still need, the 2 papers
    more, not stopping at what the older item covered."""
    held = Held(json.dumps(decision()), free=4)
    held.name = "openrouter"
    async with served(db, Config(), models=ModelGate(Models(), {"openrouter": held})):
        ids, argv, path = await _classify_left_running(conn, db, tmp_path, monkeypatch, held)
        more = classify.load(LABELS).papers[:12]
        await in_write(conn, papers_store.upsert, [paper(i, title=f"Paper {i}") for i in more])
        wider = (*argv[:-1], "12")
        again = asyncio.create_task(command(conn, tmp_path, monkeypatch, *wider))
        await asyncio.sleep(0.5)
        held.release.set()
        result = await asyncio.wait_for(again, 20)
    assert result.exit_code == 0, result.output
    first, second = await items(conn, "classify")
    assert f"item {first['id']} still writes into these outputs" in result.output
    (candidate,) = second["scope"]["candidates"]
    assert set(candidate["papers"]) == set(more) - set(ids)
    assert sorted(json.loads(x)["paper_id"] for x in jsonl.lines(path)) == sorted(more)


def test_a_result_is_written_once_by_its_name_whatever_its_text(tmp_path):
    """Two results of the same text are two lines (a charge counted twice is spent twice), and
    so are two of the same row id from two databases (a sandbox rebuilt), each item's key apart;
    one the file holds already, plain or packed, is not written again."""
    import gzip  # noqa: PLC0415

    path, line = tmp_path / "brief.jsonl", '{"paper_id": "1", "cost": 0.2}'
    at = datetime.now(UTC)
    one, other = (WorkItem(1, "command:briefs", k, k, ItemState.ENDED, at, 1) for k in "ab")
    shown = cli._Shown()
    for item, result_id in ((one, 1), (one, 2), (one, 2), (other, 1)):  # `other`: rebuilt
        shown.show({"to": str(path), "line": line}, cli._result_name(item, result_id))
    assert len(jsonl.lines(path)) == 3
    path.with_name(path.name + ".gz").write_bytes(gzip.compress(path.read_bytes()))
    path.write_text("")
    afresh = cli._result_name(one, 1)
    cli._Shown().show({"to": str(path), "line": line}, afresh)  # a new follower: it is packed
    assert jsonl.lines(path) == []


async def test_a_follower_waits_out_a_lost_database_from_where_it_was(
    conn: Session, db, monkeypatch
):
    """The database lost under a follower's read: it looks again after its pause, showing each
    result once, and so does the wait for a `serve`."""
    import psycopg  # noqa: PLC0415

    submitted = await in_write(conn, queue.submit, "command:ping", "k")
    for text in ("one", "two"):
        await conn.execute(
            "INSERT INTO work_results (work_id, body) VALUES (%s, %s)",
            (submitted.id, json.dumps({"say": text})),
        )
    await conn.execute("UPDATE work_items SET state = 'ended', outcome = 'done' WHERE id = %s",
                       (submitted.id,))  # fmt: skip
    real_results, real_serving, lost = queue.results, queue.serving, []

    async def results(*args, **kwargs):
        if not lost:
            lost.append(1)
            raise psycopg.OperationalError("the server closed the connection unexpectedly")
        return await real_results(*args, **kwargs)

    async def serving(*args, **kwargs):
        if len(lost) < 2:
            lost.append(2)
            raise psycopg.OperationalError("the server closed the connection unexpectedly")
        return await real_serving(*args, **kwargs)

    printed = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=printed))
    monkeypatch.setattr(cli, "FOLLOW_SECONDS", 0.01)
    monkeypatch.setattr(queue, "results", results)
    ended = await cli._follow(db, submitted.id)
    assert ended.outcome == Outcome.DONE
    out = printed.getvalue()
    assert out.count("one") == 1 and out.count("two") == 1 and "database was lost" in out
    async with served(db, Config()):
        monkeypatch.setattr(queue, "serving", serving)
        await cli._until_served(db, "command:ping")
    assert lost == [1, 2]


async def test_a_nightly_whose_close_lost_the_database_ends_as_stopped():
    """The nightly's close lost the database (`UnclosedError` from a lost connection): the item
    ends as `serve` stopped under it, so its command submits it again; any other failure to close
    fails it."""
    import psycopg  # noqa: PLC0415

    from litwatch.gates.work import retryable  # noqa: PLC0415
    from litwatch.jobs.context import UnclosedError  # noqa: PLC0415

    item = WorkItem(1, "command:nightly", "k", "k", ItemState.RUNNING, datetime.now(UTC), 1,
                    {"fetch": False, "days": None, "limit": 5, "digests": False})  # fmt: skip
    s = commands.Serving(cast(Any, None), Config(), cast(Any, None))
    for cause, stops in ((psycopg.OperationalError("lost"), True), (ValueError("bad"), False)):

        async def run(*args, cause=cause, **kwargs):
            raise UnclosedError(7, None) from cause

        with pytest.MonkeyPatch.context() as m:
            m.setattr(commands.nightly_job, "run", run)
            with pytest.raises(Exception) as raised:
                await commands._nightly(s, item, cast(Any, None))
        assert retryable(raised.value) == stops
        assert isinstance(raised.value, UnclosedError) != stops


async def test_a_command_on_a_blank_database_says_to_start_serve(
    conn: Session, tmp_path, monkeypatch
):
    """A database never migrated, no `serve` ever started on it: the command is refused as with
    no `serve`, not with an undefined table."""
    await conn.execute("CREATE SCHEMA blank_p5")
    try:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(
            "LITWATCH_DATABASE_URL", f"{conn.test_dsn}?options=-csearch_path%3Dblank_p5"
        )
        (tmp_path / "litwatch.toml").write_text(CONFIG_TOML)
        monkeypatch.setenv("LITWATCH_CONFIG", str(tmp_path / "litwatch.toml"))
        for argv in (["ping", "brief"], ["nightly", "--limit", "5"]):
            ran = await asyncio.to_thread(CliRunner().invoke, cli.app, argv)
            assert ran.exit_code != 0 and "no `litwatch serve` runs" in ran.output, ran.output
    finally:
        await conn.execute("DROP SCHEMA blank_p5 CASCADE")


async def test_yes_to_cancel_an_item_that_ended_done_meanwhile_exits_as_it_ended(
    conn: Session, db, tmp_path, monkeypatch
):
    """Ctrl-C, then yes to cancel, but the ping's item ended done before the cancel reached it:
    the command shows its answer and exits 0."""
    held, loop = Held(), asyncio.get_running_loop()
    interrupted(monkeypatch, held.started)

    def confirm(*args, **kwargs) -> bool:  # the item ends while the owner reads the question
        loop.call_soon_threadsafe(held.release.set)
        while (
            asyncio.run_coroutine_threadsafe(items(conn, "ping"), loop).result()[0]["state"]
            != "ended"
        ):
            threading.Event().wait(0.05)
        return True

    monkeypatch.setattr(cli.typer, "confirm", confirm)
    async with served(db, Config(), models=ModelGate(PING, {"anthropic": held})):
        result = await command(conn, tmp_path, monkeypatch, "ping", "brief")
    assert result.exit_code == 0, result.output
    assert "test-brief -> 42" in result.output and "ended done before the cancel" in result.output


async def test_serves_startup_ends_once_commands_are_served_and_raises_a_failed_registration(
    conn: Session, db, monkeypatch
):
    """The lifespan returns only once the Work gate's `workers` row is written, so a command
    run right after `serve` starts finds it; a registration that fails fails the startup."""
    async with served(db, Config()):
        found = await rows(conn, "SELECT kinds FROM workers")
        assert len(found) == 1 and "command:ping" in found[0]["kinds"]

    async def refused(*args, **kwargs):
        raise ValueError("the workers table is not this image's")

    monkeypatch.setattr(queue, "register", refused)
    with pytest.raises(ValueError, match="not this image's"):
        async with served(db, Config()):
            pass


@pytest.mark.usefixtures("short_leases")
async def test_a_reattached_evaluation_whose_item_stops_waits_for_serve_and_goes_on(
    conn: Session, db, tmp_path, monkeypatch
):
    """Run again while its item still runs, the command follows that item; `serve` stops under
    it, so it ends stopped, and the command waits for the next `serve`, as a fresh command
    does, rather than refuse, then submits what its file still lacks."""
    held = Held(json.dumps(decision()), free=4)
    held.name = "openrouter"
    models = ModelGate(Models(), {"openrouter": held})
    async with served(db, Config(), models=models):
        ids, argv, path = await _classify_left_running(conn, db, tmp_path, monkeypatch, held)
        again = asyncio.create_task(command(conn, tmp_path, monkeypatch, *argv))
        await asyncio.sleep(0.5)  # it follows the item left running
    (first,) = await items(conn, "classify")
    assert first["error"] == queue.STOPPED and not again.done()
    (cut,) = await rows(conn, "SELECT error FROM runs WHERE work_id = %s", first["id"])
    assert cut["error"] == queue.STOPPED  # its run says why, not an empty cancellation
    await asyncio.sleep(2.5)  # no `serve`: it waits
    assert not again.done()
    async with served(db, Config(), models=models):
        held.release.set()
        result = await asyncio.wait_for(again, 20)
    assert result.exit_code == 0, result.output
    assert f"item {first['id']}: {queue.STOPPED}; submitted again" in result.output
    _, second = await items(conn, "classify")
    assert second["outcome"] == "done"
    assert sorted(json.loads(x)["paper_id"] for x in jsonl.lines(path)) == sorted(ids)


async def test_a_replacement_submission_that_lost_the_database_before_its_commit_is_made_again(
    conn: Session, db, tmp_path, monkeypatch
):
    """A submission after a stop (`resumed`) that loses the database before its commit was
    tried, reading the `workers` row, is made again: the command goes on."""
    import psycopg  # noqa: PLC0415

    real, lost = queue.served_here, []

    async def served_here(*args, **kwargs):
        if not lost:
            lost.append(1)
            raise psycopg.OperationalError("the server closed the connection unexpectedly")
        return await real(*args, **kwargs)

    monkeypatch.setattr(queue, "served_here", served_here)
    monkeypatch.setattr(cli, "FOLLOW_SECONDS", 0.05)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "LITWATCH_DATABASE_URL", f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    )
    async with served(db, Config(), models=ModelGate(PING, {"anthropic": Held(free=1)})):
        await asyncio.to_thread(
            cli._submit, "ping", {"role": "brief"}, command="ping", resumed=True
        )
    (item,) = await items(conn, "ping")
    assert item["outcome"] == "done" and lost == [1]


@pytest.mark.parametrize(
    "argv",
    [
        ("eval", "classify", "decider", "--candidates", "candidates.toml"),
        ("eval", "replay", "--resume", "1", "--yes"),
        ("eval", "replay", "--yes"),
    ],
)
async def test_an_evaluation_on_a_blank_database_is_refused_before_it_prepares(
    conn: Session, tmp_path, monkeypatch, argv
):
    """An evaluation on a database never migrated is refused as having no `serve`, before it
    prepares anything, and its preparation migrates nothing."""
    await conn.execute("CREATE SCHEMA blank_p5e")
    try:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "candidates.toml").write_text('[decider]\nprovider = "openrouter"\n'
                                                  'model = "m"\n')  # fmt: skip
        monkeypatch.setenv(
            "LITWATCH_DATABASE_URL", f"{conn.test_dsn}?options=-csearch_path%3Dblank_p5e"
        )
        (tmp_path / "litwatch.toml").write_text(CONFIG_TOML)
        monkeypatch.setenv("LITWATCH_CONFIG", str(tmp_path / "litwatch.toml"))
        ran = await asyncio.to_thread(CliRunner().invoke, cli.app, list(argv))
        assert ran.exit_code != 0 and "no `litwatch serve` runs" in ran.output, ran.output
        tables = await rows(
            conn, "SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'blank_p5e'"
        )
        assert tables[0]["n"] == 0
    finally:
        await conn.execute("DROP SCHEMA blank_p5e CASCADE")


async def test_serves_startup_fails_when_its_gate_cannot_register_in_time(db, monkeypatch):
    """The database lost for good after the sweeps: registration tries again without end, so
    the startup fails once `REGISTER_SECONDS` have passed, and its cleanup cancels the gate."""
    import psycopg  # noqa: PLC0415

    from litwatch.gates import work as work_gate  # noqa: PLC0415

    async def lost(*args, **kwargs):
        raise psycopg.OperationalError("the server closed the connection unexpectedly")

    monkeypatch.setattr(queue, "register", lost)
    monkeypatch.setattr(work_gate, "RETRY_SECONDS", 0.05)
    monkeypatch.setattr(app_module, "REGISTER_SECONDS", 0.5)
    with pytest.raises(TimeoutError, match="did not register"):
        async with asyncio.timeout(10), served(db, Config()):
            pass


async def test_a_replacement_whose_commit_rolled_back_is_made_again(
    conn: Session, db, tmp_path, monkeypatch
):
    """A submission after a stop whose session the server ended before its commit (the idle
    transaction timed out, SQLSTATE 25P03): the transaction is known to have rolled back, so
    the submission is made again, once."""
    real, timed_out = queue.submit, []

    async def submit(unit, *args, **kwargs):
        item = await real(unit, *args, **kwargs)
        if not timed_out:
            timed_out.append(1)
            await unit.execute("SET LOCAL idle_in_transaction_session_timeout = '10ms'")
            await asyncio.sleep(0.3)  # the server ends the session before the commit
        return item

    monkeypatch.setattr(queue, "submit", submit)
    monkeypatch.setattr(cli, "FOLLOW_SECONDS", 0.05)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "LITWATCH_DATABASE_URL", f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    )
    async with served(db, Config(), models=ModelGate(PING, {"anthropic": Held(free=1)})):
        await asyncio.to_thread(
            cli._submit, "ping", {"role": "brief"}, command="ping", resumed=True
        )
    (item,) = await items(conn, "ping")
    assert item["outcome"] == "done" and timed_out == [1]


async def test_an_evaluation_with_no_serve_still_drains_what_its_items_answered(
    conn: Session, tmp_path, monkeypatch
):
    """With `serve` stopped, `eval classify` whose output names a failed item that answered
    its one paper: the answer is written into the file, nothing is left to ask, and no `serve`
    is needed for that (a rollback's drain)."""
    from litwatch.config import RoleConfig as Role  # noqa: PLC0415
    from litwatch.seeds import taxonomy  # noqa: PLC0415

    (pid,) = classify.load(LABELS).papers[:1]
    await in_write(conn, papers_store.upsert, [paper(pid, title="Paper")])
    (tmp_path / "candidates.toml").write_text(
        f'[decider]\nprovider = "openrouter"\nmodel = "{CLASSIFY.model}"\n'
    )
    asked = classify.fingerprint(
        Models(classify=Role(provider="openrouter", model=CLASSIFY.model)), taxonomy()
    )
    out = tmp_path / "runs"
    path = out / "decider.jsonl"
    item = await in_write(conn, queue.submit, "command:classify", "old")
    answer = classify.Result(paper_id=pid, request=asked).model_dump_json()
    await conn.execute(
        "INSERT INTO work_results (work_id, body) VALUES (%s, %s)",
        (item.id, json.dumps({"to": str(path.resolve()), "line": answer})),
    )
    await conn.execute(
        "UPDATE work_items SET state = 'ended', outcome = 'failed', error = 'x' WHERE id = %s",
        (item.id,),
    )
    cli._owned([cli._sidecar(path.resolve())], "command:classify", "old")
    argv = ("eval", "classify", "decider", "--candidates", str(tmp_path / "candidates.toml"),
            "--labels", str(LABELS), "--out", str(out), "--limit", "1")  # fmt: skip
    result = await command(conn, tmp_path, monkeypatch, *argv)
    assert result.exit_code == 0, result.output
    assert "nothing left to ask" in result.output
    assert [json.loads(x)["paper_id"] for x in jsonl.lines(path)] == [pid]
