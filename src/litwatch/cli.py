"""The jobs and a few queries from a terminal."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn

import click
import typer
import yaml
from rich.console import Console

from litwatch import config as configuration
from litwatch import logs, seeds, store
from litwatch.core import Paper, Taxonomy
from litwatch.core.work import ItemState, Outcome, WorkItem
from litwatch.feedback import word
from litwatch.gates.context import Attribution, attributed
from litwatch.gates.database import (
    BusyError,
    DatabaseGate,
    PoolTimeoutError,
    ReadUnit,
    WriteUnit,
)
from litwatch.gates.work import LEASE, retryable
from litwatch.jobs import commands
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import work as queue

app = typer.Typer(no_args_is_help=True, add_completion=False, help="litwatch")
db_app = typer.Typer(no_args_is_help=True, help="Database migrations.")
eval_app = typer.Typer(no_args_is_help=True, help="Dry runs measured against the owner's labels.")
library_app = typer.Typer(no_args_is_help=True, help="Effective grades, rarity and the library.")
taxonomy_app = typer.Typer(no_args_is_help=True, help="The class tree and its topics, as YAML.")
intake_app = typer.Typer(help="Take papers in: from arXiv, or a window from the archive.")
digests_app = typer.Typer(no_args_is_help=True, help="Daily and weekly digests.")
deep_app = typer.Typer(
    no_args_is_help=True, help="What Deep did: attempts, traces and process notes. Read-only."
)
work_app = typer.Typer(no_args_is_help=True, help="The work items commands leave to `serve`.")
app.add_typer(db_app, name="db")
app.add_typer(eval_app, name="eval")
app.add_typer(library_app, name="library")
app.add_typer(taxonomy_app, name="taxonomy")
app.add_typer(intake_app, name="intake")
app.add_typer(digests_app, name="digests")
app.add_typer(deep_app, name="deep")
app.add_typer(work_app, name="work")
console = Console()


@app.callback()
def _dotenv() -> None:
    # Before any command, in a group or not, reads its configuration: a variable kept in .env
    # (LITWATCH_DEBUG, the database, the archive) counts for all of them.
    configuration.load_dotenv()


ConfigPath = Annotated[
    Path | None, typer.Option("--config", help="litwatch.toml; default $LITWATCH_CONFIG or ./")
]
Since = Annotated[datetime, typer.Option(formats=["%Y-%m-%d"], help="The first day.")]
Until = Annotated[datetime, typer.Option(formats=["%Y-%m-%d"], help="The last day.")]


def _run[T](
    work: Callable[[DatabaseGate], Awaitable[T]],
    *,
    name: str,
    migrate: bool = True,
    db: DatabaseGate | None = None,
    attribute: bool = True,
) -> T:
    """Run `work` with the command's Database gate, the process's one, on the configured
    database, migrated first: `db` when the command built it already (unopened), else a new
    one. The gate is open for the run and closed at its end. Its gate calls are the request
    `cli <name>` (`attribute`; `name` the command's, as `eval report`): `serve` attributes none
    here, but its own, per request and item."""
    logs.setup()

    async def main() -> T:
        async with db or DatabaseGate(configuration.database_url()) as gate:
            if migrate:
                await _migrate(gate)
            return await work(gate)

    try:
        with (
            attributed(Attribution.request(f"cli {name}"))
            if attribute
            else contextlib.nullcontext()
        ):
            return asyncio.run(main())
    except (store.LegacyDatabaseError, BusyError, PoolTimeoutError) as exc:
        raise SystemExit(str(exc)) from exc


# -- commands `serve` runs: submitted, then followed --------------------------------------------

# How often a command following its item looks at it when no notification comes.
FOLLOW_SECONDS = 2.0

type Opening = Callable[[WriteUnit], Awaitable[dict[str, Any]]]
type Again = Callable[[dict[str, Any]], dict[str, Any] | None]


class _Followed:
    """What a command has shown of the items it followed: the last result shown of each, and
    the lines its files hold (`_Shown`); and the item it submitted last (`kind`, `key`, and the
    item once its submission returned), which Ctrl-C asks about."""

    def __init__(self) -> None:
        self.shown = _Shown()
        self.last: dict[int, int] = {}
        self.kind: str | None = None
        self.key: str | None = None
        self.item: WorkItem | None = None


class _UnservedError(Exception):
    """No `serve` runs the kind, as the submitting unit found it."""


def _submit(
    name: str,
    scope: dict[str, Any],
    *,
    command: str,
    admission: str | None = None,
    key: str | Callable[[dict[str, Any]], str] | None = None,
    opening: Opening | None = None,
    sidecars: Collection[Path] = (),
    again: Again | None = None,
    resumed: bool = False,
) -> None:
    """Submit the command `name`'s work item (`jobs.commands`) with `scope`, under `key` (by
    default a fresh one; a function of the scope, for a replay's), admitted by `admission` (by
    default its key), and follow it to its end (`_follow`), as the request `cli <command>`: the
    database is the one `serve` works from, and `serve` runs it. Refused, before anything is
    written, when no `serve` runs its kind: a live `workers` row, read and locked in the unit
    that submits (`store.work.served_here`). `opening` writes what the item works on in that
    unit, and gives what it adds to the scope (a replay's record). `sidecars` name the item as
    the one writing into an evaluation's outputs (`_owned`).

    An item `serve` stopped under ends failed (`STOPPED`): the command says so, waits until a
    `serve` runs its kind again, and submits a fresh item, which goes on from what the first
    committed: of the same scope, or of what `again` makes of it (an evaluation's, from what its
    files now hold; None when nothing is left). One `resumed` goes on after an item it
    reattached to ended so (`_caught_up`, `_following_replay`): it enters the same loop at the
    same step, waiting for a `serve` before it submits. A submission made after a stop that
    loses the database before its commit is tried (opening its unit, reading the `workers`
    row) is made again; one whose commit was tried is D4's. Ctrl-C asks whether to cancel the
    item or leave it to run (`_interrupted`). A failed item fails the command with its error."""
    work_kind, followed = commands.kind(name), _Followed()
    followed.kind = work_kind
    committing = False

    async def submitted(
        db: DatabaseGate, current: dict[str, Any], first: bool, refuse: bool
    ) -> WorkItem:
        nonlocal committing
        committing = False
        async with db.write(f"{name}: submit") as unit:
            if not await queue.served_here(unit, work_kind, LEASE):
                if refuse:
                    raise SystemExit(_unserved(work_kind))
                raise _UnservedError
            if opening is not None and first:
                current = current | await opening(unit)
            if key is None or isinstance(key, str):
                chosen = key if first and key else str(uuid.uuid4())
            else:
                chosen = key(current)
            followed.key = chosen
            _owned(sidecars, work_kind, chosen)
            item = await queue.submit(
                unit, work_kind, chosen, scope=current, admission=admission or chosen
            )
            committing = True  # what fails from here may have committed
        return item

    async def main(db: DatabaseGate) -> WorkItem | None:
        current, first, waits = scope, True, resumed
        while True:
            if waits:
                await _until_served(db, work_kind)
            try:
                item = followed.item = await submitted(db, current, first, refuse=not waits)
            except _UnservedError:
                continue
            except Exception as exc:
                if not waits or committing or not retryable(exc):
                    raise
                console.print(f"[yellow]the database was lost ({exc}); submitting again[/yellow]")
                await asyncio.sleep(FOLLOW_SECONDS)
                continue
            console.print(f"item {item.id}: {work_kind} (Ctrl-C to cancel it or leave it running)")
            ended = await _follow(db, item.id, followed)
            if not _stopped(ended):
                return ended
            console.print(
                f"item {ended.id}: {ended.error}; submitted again, to go on from what it did, "
                f"once a `litwatch serve` runs {work_kind}"
            )
            current, first, waits = (again(ended.scope) if again else ended.scope), False, True
            if current is None:
                console.print("nothing left to do")
                return None

    try:
        ended = _run(main, name=command, migrate=False)
    except KeyboardInterrupt:
        _interrupted(followed, command)
    _ended(ended)


def _unserved(kind: str) -> str:
    """What a command refused for want of a `serve` says."""
    return (
        f"no `litwatch serve` runs {kind} on this database: start one "
        "(docs/agents/dev-host.md); a command never runs its work itself"
    )


def _preflight(name: str, command: str) -> None:
    """Before an evaluation prepares anything: refused, read-only, when the database has no
    work queue (blank, never migrated) or no `serve` runs the command's kind. The check that
    counts is still the submit unit's (`_submit`)."""
    kind = commands.kind(name)

    async def served(db: DatabaseGate) -> bool:
        async with db.read("is it served") as unit:
            return await queue.migrated(unit) and await queue.serving(unit, kind, LEASE)

    if not _run(served, name=command, migrate=False):
        raise SystemExit(_unserved(kind))


def _stopped(item: WorkItem) -> bool:
    """Whether the item ended as `serve` stopping under it ends it (`store.work.STOPPED`)."""
    return item.outcome == Outcome.FAILED and (item.error or "").startswith(queue.STOPPED)


async def _until_served(db: DatabaseGate, kind: str) -> None:
    """Wait until a `serve` runs `kind`, looking every `FOLLOW_SECONDS`, a lost database waited
    out."""
    while True:
        with _lost_waited_out():
            async with db.read("is it served") as unit:
                if await queue.serving(unit, kind, LEASE):
                    return
        await asyncio.sleep(FOLLOW_SECONDS)


@contextlib.contextmanager
def _lost_waited_out() -> Generator[None]:
    """A follower's read that lost the database is skipped: the caller looks again, from where it
    was, after its pause. Any other error is raised."""
    try:
        yield
    except Exception as exc:
        if not retryable(exc):
            raise
        console.print(f"[yellow]the database was lost ({exc}); looking again[/yellow]")


def _ended(item: WorkItem | None) -> None:
    if item is not None and item.outcome == Outcome.FAILED:
        raise SystemExit(f"item {item.id} failed: {item.error}")


async def _follow(db: DatabaseGate, item_id: int, followed: _Followed | None = None) -> WorkItem:
    """Follow the command item `item_id` to its end, showing its progress as it arrives, from
    the last result `followed` showed of it, and then its completion record (`_Shown`): woken
    by a notification (its results', or any item's settling) and else every `FOLLOW_SECONDS`. It
    only reads: the item is `serve`'s. It says so when no `serve` runs the item's kind while the
    item waits. Returns the item, ended."""
    followed = followed or _Followed()
    shown, wake = followed.shown, asyncio.Event()
    listening = asyncio.create_task(_woken(db, wake))
    unserved, was = False, None
    try:
        while True:
            wake.clear()
            read = None
            with _lost_waited_out():
                async with db.read("follow") as unit:
                    item = await queue.get(unit, item_id)
                    if item is None or not queue.once(item.kind):
                        raise SystemExit(f"no command's work item {item_id}")
                    found = await queue.results(unit, item_id, followed.last.get(item_id, 0))
                    read = (item, found, await queue.serving(unit, item.kind, LEASE))
            if read is None:  # the database was lost: looked at again after the pause
                await asyncio.sleep(FOLLOW_SECONDS)
                continue
            item, found, served = read
            for last, body in found:
                shown.show(body, _result_name(item, last))
                followed.last[item_id] = last
            if item.state == ItemState.ENDED:
                for body in item.result or []:
                    shown.show(body)
                return item
            if item.state != was:
                if item.state == ItemState.PENDING and was == ItemState.RUNNING:
                    console.print(f"item {item_id} waits to run (the run lock is taken)")
                was = item.state
            if not served and not unserved:
                console.print(f"no `litwatch serve` runs {item.kind} now: item {item_id} waits")
            unserved = not served
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), FOLLOW_SECONDS)
    finally:
        listening.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listening


async def _woken(db: DatabaseGate, wake: asyncio.Event) -> None:
    """Set `wake` on each notification of the Work gates' channels, listening again after a
    drop; the follower's own reads cover what a drop misses."""
    while True:
        try:
            async with db.listen(queue.CHANNEL, queue.RESULTS) as notes:
                async for _ in notes:
                    wake.set()
        except Exception:
            await asyncio.sleep(FOLLOW_SECONDS)


class _Shown:
    """An item's results as the command shows them (`jobs.commands`): text and data printed,
    lines appended to their files once each, whoever followed the item before. A line carries
    its result's name (`work_result`: its item's key, a fresh uuid, and its `work_results` row's
    id, which is only this database's), by which a file that holds it, plain or packed
    (`<file>.gz`), is not written it again: two results of the same text are two lines. A
    record cut off at a file's end, by a kill mid-write, is cut off, in bytes, before the file
    is first read (`jsonl.mend`)."""

    def __init__(self) -> None:
        self.files: dict[str, set[str]] = {}

    def show(self, body: dict[str, Any], result: str | None = None) -> None:
        if "say" in body:
            console.print(body["say"], markup=False)
        elif "data" in body:
            console.print_json(data=body["data"])
        elif "to" in body:
            assert result is not None  # a file's line is a result, never a completion's
            self.line(Path(body["to"]), body["line"], result)

    def line(self, path: Path, line: str, result: str) -> None:
        from litwatch.quality import jsonl  # noqa: PLC0415

        held = self.files.get(str(path))
        if held is None:
            jsonl.mend(path)
            held = self.files[str(path)] = _result_ids(path)
        if result in held:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        record = json.loads(line) | {"work_result": result}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        held.add(result)


def _result_name(item: WorkItem, result_id: int) -> str:
    """A result's name in a file: unique across databases, as its row's id alone is not."""
    return f"{item.key}:{result_id}"


def _result_ids(path: Path) -> set[str]:
    """The results' names the file's records carry, its packed records' (`<file>.gz`)
    included."""
    import gzip  # noqa: PLC0415

    from litwatch.quality import jsonl  # noqa: PLC0415

    found = jsonl.lines(path)
    packed = path.with_name(path.name + ".gz")
    if packed.exists():
        found += jsonl.records(gzip.decompress(packed.read_bytes()), whole=True)
    return {i for x in found if (i := json.loads(x).get("work_result")) is not None}


def _interrupted(followed: _Followed, command: str) -> NoReturn:
    """Ctrl-C while a command follows its item, or waits to submit it again: ask whether to
    cancel the item or leave it to run, which `litwatch work follow` picks back up. A submission
    whose reply did not come may or may not have committed: the item is found by its key."""

    async def find(db: DatabaseGate) -> WorkItem | None:
        async with db.read("interrupted: the item") as unit:
            if followed.item is not None:
                return await queue.get(unit, followed.item.id)
            if followed.kind is None or followed.key is None:
                return None
            return await queue.find(unit, followed.kind, followed.key)

    item = _run(find, name=command, migrate=False)
    if item is None:
        raise SystemExit("stopped before its work item was submitted")
    _stop(item, command, followed)


def _stop(item: WorkItem, command: str, followed: _Followed) -> NoReturn:
    """Ask the owner whether to cancel the item followed until Ctrl-C. Cancelled, the request is
    `serve`'s to act on (`store.work.cancel`): the command follows the item to its end, its
    Worker stopped and its cleanup done, and says how it ended. Else it goes on. One that ended
    meanwhile is shown as the follower shows it, to its completion record."""
    if item.state != ItemState.ENDED:
        try:
            cancel = typer.confirm(
                f"\nCancel item {item.id}? Otherwise it goes on, and "
                f"`litwatch work follow {item.id}` picks it back up",
                default=False,
            )
        except (typer.Abort, click.Abort):
            cancel = False
        if not cancel:
            console.print(
                f"item {item.id} goes on: `litwatch work follow {item.id}` picks it back up"
            )
            raise typer.Exit(130)

        async def cancelled(db: DatabaseGate) -> WorkItem:
            async with db.write("cancel the item") as unit:
                asked = await queue.cancel(unit, item.id)
            assert asked is not None  # a command's item, followed until now
            if asked.state != ItemState.ENDED:
                console.print(f"item {item.id}: cancel asked; `serve` stops it")
            return await _follow(db, item.id, followed)

        item = _run(cancelled, name=command, migrate=False)
        if item.outcome == Outcome.DONE:  # it ended done before the cancel reached it
            console.print(f"item {item.id} ended done before the cancel reached it")
            raise typer.Exit(0)
        raise SystemExit(f"item {item.id} ended {item.outcome}: {item.error or 'done'}")
    item = _run(lambda db: _follow(db, item.id, followed), name=command, migrate=False)
    _ended(item)
    raise typer.Exit(0)


def _follow_again(item_id: int, command: str) -> WorkItem:
    """Follow the command item `item_id`, submitted before, to its end; Ctrl-C as `_stop`."""
    followed = _Followed()
    try:
        return _run(lambda db: _follow(db, item_id, followed), name=command, migrate=False)
    except KeyboardInterrupt:

        async def found(db: DatabaseGate) -> WorkItem | None:
            async with db.read("interrupted: the item") as unit:
                return await queue.get(unit, item_id)

        item = _run(found, name=command, migrate=False)
        assert item is not None  # followed until now
        _stop(item, command, followed)


@work_app.command("follow")
def work_follow(item_id: Annotated[int, typer.Argument(help="The work item's id.")]) -> None:
    """Follow a command's work item to its end, as the command did before Ctrl-C left it to
    run: its output from the start, its files' lines written once each. Only a command's: the
    other work items are `serve`'s own. One `serve` stopped under is not submitted again here:
    run its command again, which goes on from what it did."""
    ended = _follow_again(item_id, "work follow")
    if _stopped(ended):
        raise SystemExit(f"item {ended.id}: {ended.error}; run its command again to go on")
    _ended(ended)


# -- an evaluation's outputs: the item writing them, named beside them ----------------------------


def _sidecar(output: Path, *, directory: bool = False) -> Path:
    """Where the item that last wrote into `output` is named: in a directory of outputs, or
    beside one file."""
    return output / "item.json" if directory else output.with_name(output.name + ".item")


def _replayed(sidecars: Collection[Path], command: str) -> list[WorkItem]:
    """Before an evaluation decides what is left to ask: the results of the items named in
    `sidecars` (the last to write each output) written into their files (`_Shown`, once each),
    so what they spent while no command followed them counts: their answers, their
    reservations. Returns those still open."""
    named = {
        (found["kind"], found["key"])
        for path in sidecars
        if path.exists()
        for found in [json.loads(path.read_text(encoding="utf-8"))]
    }
    if not named:
        return []

    async def read(db: DatabaseGate) -> list[tuple[WorkItem, list[tuple[int, dict[str, Any]]]]]:
        found = []
        async with db.read("evaluation: earlier items") as unit:
            for kind, key in sorted(named):
                if (item := await queue.find(unit, kind, key)) is not None:
                    found.append((item, await queue.results(unit, item.id)))
        return found

    shown, still = _Shown(), []
    for item, results in _run(read, name=command, migrate=False):
        for result_id, body in results:
            if "to" in body:
                shown.show(body, _result_name(item, result_id))
        if item.state != ItemState.ENDED:
            still.append(item)
    return still


def _owned(sidecars: Collection[Path], kind: str, key: str) -> None:
    """Name the item about to be submitted (`kind`, `key`) as the one writing the outputs of
    `sidecars`: written before it is, so no item writes unnamed, each through a sibling that
    replaces it whole, so a kill leaves the old name or the new."""
    for path in sidecars:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = path.with_name(path.name + ".tmp")
        fresh.write_text(json.dumps({"kind": kind, "key": key}) + "\n", encoding="utf-8")
        fresh.replace(path)


def _caught_up(sidecars: Collection[Path], command: str) -> bool:
    """`_replayed`, and each item that still writes into the outputs followed to its end first,
    so no second item writes beside it: the command then asks what its own arguments still
    need, which an older item, of fewer papers or candidates, may have left. Whether one ended
    as `serve` stopped under it: the command's submission is then `resumed` (`_submit`)."""
    stopped = False
    for still in _replayed(sidecars, command):
        console.print(f"item {still.id} still writes into these outputs: following it first")
        ended = _follow_again(still.id, command)
        console.print(f"item {ended.id} ended {ended.outcome}: {ended.error or 'done'}")
        stopped = stopped or _stopped(ended)
    return stopped


async def _migrate(db: DatabaseGate) -> list[str]:
    # On the migration lock's own session, so the migrations cannot outlive the lock.
    with attributed(Attribution.system("startup: migrations")):
        async with db.startup("migration") as conn:
            return await store.migrate(conn)


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending migrations."""
    applied = _run(_migrate, name="db migrate", migrate=False)
    console.print("applied: " + (", ".join(applied) or "nothing, already up to date"))


@intake_app.callback(invoke_without_command=True)
def intake(
    ctx: typer.Context,
    days: Annotated[int | None, typer.Option(help="Look back this many days.")] = None,
    catalog_from: Annotated[
        datetime | None,
        typer.Option(
            formats=["%Y-%m-%d"],
            help="Run only the catalog pass, which registers the rest of arXiv with the "
            "archive, from this day.",
        ),
    ] = None,
) -> None:
    """Fetch new and revised papers for the configured categories from arXiv. `serve` runs it."""
    if ctx.invoked_subcommand is not None:
        return
    if catalog_from is not None:
        if days is not None:
            raise typer.BadParameter("--days and --catalog-from do not go together")
        _submit("intake-catalog", {"since": catalog_from.date().isoformat()}, command="intake")
        return
    _submit("intake", {"days": days}, command="intake")


@intake_app.command("archive")
def intake_archive(
    since: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], help="First day.")] = None,
    until: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Last day; default today (UTC).")
    ] = None,
    days: Annotated[int | None, typer.Option(help="Instead of --since: this many days.")] = None,
    here: Annotated[
        bool,
        typer.Option(
            "--here",
            help="Run it in this process, for a database no `serve` works from (a fresh "
            "sandbox's): it calls no model.",
        ),
    ] = False,
    config: Annotated[
        Path | None, typer.Option("--config", help="With --here: litwatch.toml.")
    ] = None,
) -> None:
    """Read the papers first submitted in a window, in the configured categories, from the
    archive ($LITWATCH_ARCHIVE_URL and $LITWATCH_ARCHIVE_TOKEN, or the archive integration).
    `serve` runs it, under its configuration; with --here, this process does."""
    if (since is None) == (days is None) or (days is not None and days < 1):
        raise typer.BadParameter("give --since or a positive --days")
    if config is not None and not here:
        raise typer.BadParameter("--config goes with --here: `serve` runs it under its own")
    last = until.date() if until else datetime.now(UTC).date()
    first = since.date() if since else last - timedelta(days=days - 1)
    if first > last:
        raise typer.BadParameter("--since must not follow --until")
    if not here:
        scope = {"first": first.isoformat(), "last": last.isoformat()}
        _submit("intake-archive", scope, command="intake archive")
        return
    from litwatch.integrations import archive as archive_integration  # noqa: PLC0415
    from litwatch.jobs import intake as intake_job  # noqa: PLC0415

    cfg = configuration.load(config)

    async def here_(db: DatabaseGate) -> Any:
        # No Work gate and no Model gate: it calls no model, and only this process runs it.
        client = archive_integration.source(cfg)
        try:
            return await intake_job.from_archive(db, cfg, client, first, last)
        finally:
            await client.aclose()

    console.print(commands.archived(first, last, _run(here_, name="intake archive --here")))


@app.command()
def nightly(
    fetch: Annotated[bool, typer.Option(help="Harvest arXiv first.")] = True,
    days: Annotated[int | None, typer.Option(help="Look back this many days.")] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            help="At most this many papers per step (a trial run), per work item: one submitted "
            "again after `serve` stops counts afresh."
        ),
    ] = None,
    digests: Annotated[
        bool,
        typer.Option(
            help="Wait for the full-text passes and write today's digests; the restart says "
            "--no-digests and writes its window's with `digests write`."
        ),
    ] = True,
) -> None:
    """The nightly run as the scheduler runs it: intake, classification, analysis, grading,
    the full-text passes and the digests. `serve` runs it, under its configuration."""
    scope = {"fetch": fetch, "days": days, "limit": limit, "digests": digests}
    _submit("nightly", scope, command="nightly")


# -- digests --------------------------------------------------------------------------------------


@digests_app.command("write")
def digests_write(since: Since, until: Until) -> None:
    """Write the digests of every day from --since to --until, and the weekly digests of the
    weeks inside it, as the nightly run would have on those days, over the papers due now
    (the restart, a missed night). Published digests stay as they are. `serve` runs it."""
    scope = {"since": since.date().isoformat(), "until": until.date().isoformat()}
    _submit("digests", scope, command="digests write")


# -- deep ------------------------------------------------------------------------------------------


@deep_app.command("trace")
def deep_trace(
    paper: Annotated[str, typer.Argument(help="The paper's arXiv id.")],
    attempt: Annotated[
        int | None, typer.Option(help="The attempt, numbered from 1 as listed; default the latest.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Everything, uncut, as JSON.")] = False,
    config: ConfigPath = None,
) -> None:
    """The paper's Deep attempts, then one attempt's trace: its turns, cells, host calls with
    why one failed and where a fetch went, submits, its review rounds with their scores and
    findings, and the card's process notes if it wrote the card. Times are the configured time
    zone's."""
    import json  # noqa: PLC0415

    from litwatch.fulltext import deep_trace as text  # noqa: PLC0415
    from litwatch.store import integrations as cards  # noqa: PLC0415

    zone = configuration.load(config).schedule.zone

    async def work(db: DatabaseGate) -> tuple[Any, ...]:
        async with db.read("deep trace") as unit:
            return await trace_of(unit)

    async def trace_of(conn: ReadUnit) -> tuple[Any, ...]:
        found = await cards.attempts(conn, paper)
        if not found:
            raise SystemExit(f"{paper} has no Deep attempts")
        chosen = len(found) if attempt is None else attempt
        if not 1 <= chosen <= len(found):
            raise SystemExit(f"{paper} has attempts 1 to {len(found)}")
        trace = await cards.attempt_trace(conn, found[chosen - 1]["id"])
        reviews = await cards.attempt_reviews(conn, found[chosen - 1]["id"])
        known = await papers_store.get(conn, paper)
        notes = []
        # The card's process notes are this attempt's only when it wrote the card.
        if found[chosen - 1]["wrote_card"]:
            card = await cards.card(conn, paper)
            notes = ((card or {}).get("card") or {}).get("process_notes") or []
        return found, chosen, trace, known.title if known else None, notes, reviews

    found, chosen, trace, title, notes, reviews = _run(work, name="deep trace", migrate=False)
    if as_json:
        out = {"paper": paper, "title": title, "attempts": found, "attempt": chosen}
        out |= {"trace": trace, "reviews": reviews, "process_notes": notes}
        print(json.dumps(out, indent=2, default=str))
        return
    # Plain text: a trace holds brackets that rich would read as markup.
    print(text.trace_text(paper, title, found, chosen, trace, notes, zone, reviews), end="")


@deep_app.command("input")
def deep_input(
    text: Annotated[
        Path, typer.Argument(help="The paper's text: its LaTeX source, inputs inlined, or a PDF's.")
    ],
    paper_id: Annotated[str, typer.Option("--id", help="The paper's arXiv id.")],
    title: Annotated[str, typer.Option(help="The paper's title.")],
    abstract: Annotated[str, typer.Option(help="The paper's abstract.")] = "",
    pdf: Annotated[bool, typer.Option("--pdf", help="The text is a PDF's, not LaTeX.")] = False,
) -> None:
    """Print what the deep service is sent to read a paper's text, as `node src/main.ts run
    FILE.json` in deep/ reads it: the text, and a LaTeX source's outline as litwatch parses it.
    It reads no database and calls no service."""
    from litwatch.archive import PaperText  # noqa: PLC0415
    from litwatch.integrations.deep import job_input  # noqa: PLC0415

    paper = Paper(id=paper_id, title=title, abstract=abstract)
    body = PaperText(text.read_text(), None, "pdf" if pdf else "latex")
    typer.echo(job_input(paper, body).model_dump_json(exclude_none=True, indent=2))


@deep_app.command("notes")
def deep_notes(
    since: Annotated[
        datetime | None,
        typer.Option(formats=["%Y-%m-%d"], help="Cards written on or after this day."),
    ] = None,
    config: ConfigPath = None,
) -> None:
    """The cards' process notes, newest card first, each under its paper. A day is the
    configured time zone's."""
    from litwatch.fulltext import deep_trace as text  # noqa: PLC0415
    from litwatch.store import integrations as cards  # noqa: PLC0415

    zone = configuration.load(config).schedule.zone
    start = since.replace(tzinfo=zone) if since is not None else None

    async def work(db: DatabaseGate) -> Any:
        async with db.read("deep notes") as unit:
            return await cards.process_notes(unit, start)

    found = _run(work, name="deep notes", migrate=False)
    print(text.notes_text(found, zone), end="")


# -- the taxonomy ----------------------------------------------------------------------------------


def _taxonomy_file(path: Path | None) -> Taxonomy:
    try:
        return seeds.taxonomy(path)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


@taxonomy_app.command("import")
def taxonomy_import(
    path: Annotated[Path | None, typer.Argument(help="A taxonomy YAML; default the seed.")] = None,
    config: ConfigPath = None,
) -> None:
    """Make the file the taxonomy. A changed class or topic gets a new revision and applies
    forward; a class left out is retired, a topic left out unfollowed."""
    rules = word.rules(configuration.load(config))  # its papers are settled by them
    tree = _taxonomy_file(path)

    async def work(db: DatabaseGate) -> taxonomy_store.Saved:
        async with db.write("import taxonomy", taxonomy="exclusive") as unit:
            return await word.save_taxonomy(unit, tree, rules=rules)

    saved = _run(work, name="taxonomy import")
    console.print(f"{len(tree.classes)} classes, {len(tree.topics)} topics")
    for what, slugs in (
        ("new revisions of classes", saved.revised_classes),
        ("retired classes", saved.retired_classes),
        ("new revisions of topics", saved.revised_topics),
        ("unfollowed topics", saved.unfollowed_topics),
    ):
        if slugs:
            console.print(f"{what}: {', '.join(slugs)}")


@taxonomy_app.command("export")
def taxonomy_export(
    path: Annotated[Path | None, typer.Argument(help="Default: standard output.")] = None,
) -> None:
    """Write the taxonomy in the database as YAML, in the format `import` reads."""

    async def work(db: DatabaseGate) -> Taxonomy | None:
        async with db.read("export taxonomy") as unit:
            return await taxonomy_store.load(unit)

    tree = _run(work, name="taxonomy export")
    if tree is None:
        raise SystemExit("no taxonomy yet: `litwatch taxonomy import` the seed first")
    text = seeds.dump_taxonomy(tree)
    if path is None:
        typer.echo(text, nl=False)
    else:
        path.write_text(text)


@taxonomy_app.command("check")
def taxonomy_check(
    path: Annotated[Path | None, typer.Argument(help="A taxonomy YAML; default the seed.")] = None,
) -> None:
    """Validate a taxonomy file and summarise it."""
    tree = _taxonomy_file(path)
    levels = Counter(tree.level(c.slug) for c in tree.classes)
    console.print(
        f"{len(tree.classes)} classes ("
        + ", ".join(f"{n} at level {level}" for level, n in sorted(levels.items()))
        + ")"
    )
    for topic in tree.topics:
        console.print(f"{topic.depth.value:6} {topic.monogram}  {tree.get(topic.slug).name}")


# -- evaluation ------------------------------------------------------------------------------------


@eval_app.command("classify")
def eval_classify(
    candidate: Annotated[list[str], typer.Argument(help="Candidates named in --candidates.")],
    candidates: Annotated[
        Path, typer.Option(help="TOML: one table per candidate, each a role.")
    ] = Path("evals/configs/classify.toml"),
    labels: Annotated[Path, typer.Option(help="The evaluation labels.")] = Path("evals/labels"),
    out: Annotated[Path, typer.Option(help="One <candidate>.jsonl per candidate.")] = Path(
        "evals/runs/classify"
    ),
    limit: Annotated[int | None, typer.Option(help="Only the first N papers.")] = None,
    concurrency: int = 8,
) -> None:
    """Sort the evaluation papers with each candidate as the `classify` role. A dry run: it reads
    titles and abstracts from the configured database and writes its answers only to `out`, as
    `serve` answers. Its calls are journalled on an evaluation run: production's reuse and price
    estimates leave them out, and only `eval replay`'s estimate reads them. A run resumes where
    it stopped, asking only what `out` does not hold, and so does an item `serve` stopped under,
    submitted again. Each candidate's file names the item that last wrote it
    (`<candidate>.jsonl.item`): a run first writes in what that item answered while no command
    followed it, and follows it to its end first while it still runs; then it asks what
    its own arguments still need."""
    import tomllib  # noqa: PLC0415

    from litwatch.config import Models, RoleConfig  # noqa: PLC0415
    from litwatch.quality import classify, jsonl  # noqa: PLC0415

    command = "eval classify"
    roles = tomllib.loads(candidates.read_text())
    if unknown := set(candidate) - set(roles):
        raise typer.BadParameter(f"no candidate {', '.join(sorted(unknown))} in {candidates}")
    paths = {name: (out / f"{name}.jsonl").resolve() for name in candidate}
    sidecars = [_sidecar(path) for path in paths.values()]
    _preflight("classify", command)
    resumed = _caught_up(sidecars, command)
    tree, evaluation = seeds.taxonomy(), classify.load(labels)
    ids = evaluation.papers[:limit]
    found = {p.id for p in _run(lambda db: _papers(db, ids), name=command, migrate=False)}
    if missing := len(ids) - len(found):
        console.print(f"[yellow]{missing} papers are not in the database and are left out[/yellow]")

    def left() -> list[dict[str, Any]]:
        """Each candidate's papers its file does not answer."""
        chosen = []
        for name, path in paths.items():
            role = RoleConfig.model_validate(roles[name])
            asked = classify.fingerprint(Models(classify=role), tree)
            jsonl.mend(path)
            try:
                todo = classify.left(path, asked, [i for i in ids if i in found])
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            chosen.append(
                {"name": name, "role": role.model_dump(mode="json"), "papers": todo,
                 "out": str(path)}
            )  # fmt: skip
        return chosen

    def again(scope: dict[str, Any]) -> dict[str, Any] | None:
        chosen = left()
        return scope | {"candidates": chosen} if any(c["papers"] for c in chosen) else None

    scope = {"tree": tree.model_dump(mode="json"), "concurrency": concurrency}
    if (first := again(scope)) is None:
        console.print("nothing left to ask: each file holds its answers")
        return
    _submit("classify", first, command=command, sidecars=sidecars, again=again, resumed=resumed)


@eval_app.command("classify-report")
def eval_classify_report(
    candidate: Annotated[list[str], typer.Argument(help="Candidates whose runs to compare.")],
    labels: Annotated[Path, typer.Option(help="The evaluation labels.")] = Path("evals/labels"),
    runs: Annotated[Path, typer.Option(help="Where the runs are.")] = Path("evals/runs/classify"),
) -> None:
    """The comparison's tables, as markdown, from the runs alone: no calls."""
    from litwatch.quality import classify  # noqa: PLC0415

    results = {n: classify.results(runs / f"{n}.jsonl") for n in candidate}
    print(classify.report(seeds.taxonomy(), classify.load(labels), results), end="")


def _counts(pairs: list[str], name: str) -> dict[str, int]:
    """`COHORT=N` options as a mapping."""
    found = {}
    for pair in pairs:
        cohort, _, n = pair.partition("=")
        if not n.isdigit():
            raise typer.BadParameter(f"{pair!r}: expected COHORT=N", param_hint=name)
        found[cohort] = int(n)
    return found


@eval_app.command("replay")
def eval_replay(
    cohort: Annotated[
        list[str] | None,
        typer.Option(help="Only these cohorts: owner, provisional, legacy, silver, miss."),
    ] = None,
    negatives: Annotated[
        list[str] | None,
        typer.Option(
            help="COHORT=N: at most N of the cohort's negatives, drawn with a fixed seed."
        ),
    ] = None,
    labels: Annotated[
        Path | None, typer.Option(help="The evaluation labels. Default: evals/labels.")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Only the first N papers.")] = None,
    resume: Annotated[int | None, typer.Option(help="Go on with this replay.")] = None,
    note: Annotated[str | None, typer.Option(help="A note kept with the run.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Spend without asking.")] = False,
    config: ConfigPath = None,
) -> None:
    """Run the labelled and missed papers through the current classification, keyword check
    and grading (feedback.md, evaluation). A dry run: it writes to the evaluation tables only.
    Shows what it would cost and asks first; `serve` runs it, under its configuration, which
    must be the one estimated with (--config); a run cut short goes on with --resume. A replay
    has one item open at a time (keyed by its run): --resume follows the one still open first,
    and an item `serve` stopped under is submitted again, going on from what it recorded."""
    from litwatch.quality import replay  # noqa: PLC0415

    _preflight("replay", "eval replay")
    resumed = False
    if resume is not None and (ended := _following_replay(resume)) is not None:
        if not _stopped(ended):
            _ended(ended)
            return
        resumed = True
    cfg = configuration.load(config)
    sample = _counts(negatives or [], "--negatives")
    if unknown := (set(cohort or []) | set(sample)) - set(replay.COHORTS):
        raise typer.BadParameter(f"no cohort {', '.join(sorted(unknown))}")

    async def prepare(db: DatabaseGate) -> tuple[Any, dict[str, float]]:
        # The owner's label files are read before the unit opens, not while it holds a snapshot.
        given = replay.load(labels or replay.LABELS) if resume is None else []
        async with db.read("replay: prepare") as unit:
            if resume is not None:
                r = await replay.resume(unit, cfg, resume)
            else:
                tree = await taxonomy_store.load(unit)
                if tree is None:
                    raise ValueError("no taxonomy yet: `litwatch taxonomy import` the seed first")
                every = given + await replay.misses(unit)
                chosen = replay.select(every, replay.focus(tree), cohorts=cohort, negatives=sample)
                r = await replay.prepare(unit, chosen, limit=limit)
            return r, await replay.estimate(unit, cfg, r)

    try:
        r, cost = _run(prepare, name="eval replay", migrate=False)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if r.left_out:
        console.print(f"[yellow]{len(r.left_out)} labelled papers are not in the database and "
                      "are left out[/yellow]")  # fmt: skip
    if r.stale:
        console.print(f"{len(r.stale)} papers' text changed since the run read them: both stages "
                      "run again on them")  # fmt: skip
    by_cohort = Counter(x.cohort for x in r.labels)
    console.print(
        f"{len(r.labels)} labels ({', '.join(f'{c} {n}' for c, n in by_cohort.items())}) on "
        f"{len(r.papers)} papers; to do: classify {len(r.to_classify)} papers, grade "
        f"{len(r.to_grade)} paper-topic pairs; estimated ${sum(cost.values()):.2f} "
        f"(classification ${cost['classification']:.2f}, grading ${cost['grading']:.2f})"
    )
    if not r.to_classify and not r.to_grade:
        return
    if not yes and not typer.confirm("Spend it?"):
        raise typer.Exit(1)

    async def start(unit: WriteUnit) -> dict[str, Any]:
        # The replay's record, in the unit that submits its item: none without the item.
        params = {"cohorts": cohort, "negatives": sample, "seed": replay.SEED, "limit": limit}
        return {"eval_run_id": await replay.start(unit, cfg, r, params, note)}

    def keyed(scope: dict[str, Any]) -> str:
        return str(scope["eval_run_id"])  # one item open per replay: a second is the same

    if r.eval_run_id is None:
        _submit("replay", {}, command="eval replay", opening=start, key=keyed)
        return
    scope = {"eval_run_id": r.eval_run_id}
    _submit("replay", scope, command="eval replay", key=keyed, resumed=resumed)


def _following_replay(eval_run_id: int) -> WorkItem | None:
    """Follow the replay `eval_run_id`'s open item to its end, if it has one, as the item
    writing into it (as `_caught_up` follows one): the item, ended; None when none was open.
    One that ended as `serve` stopped under it leaves the command to go on, `resumed`."""

    async def found(db: DatabaseGate) -> WorkItem | None:
        async with db.read("replay: its item") as unit:
            return await queue.find(unit, commands.kind("replay"), str(eval_run_id))

    still = _run(found, name="eval replay", migrate=False)
    if still is None or still.state == ItemState.ENDED:
        return None
    console.print(f"item {still.id} still runs replay {eval_run_id}: following it")
    ended = _follow_again(still.id, "eval replay")
    if _stopped(ended):
        console.print(f"item {ended.id}: {ended.error}; going on from what it recorded")
    return ended


@eval_app.command("report")
def eval_report(
    eval_run: Annotated[int | None, typer.Argument(help="Default: the latest replay.")] = None,
    out: Annotated[Path | None, typer.Option(help="Also write the report here.")] = None,
) -> None:
    """A replay's recall per Focus topic and cohort, as markdown, from what it recorded: no
    calls."""
    from litwatch.core.evaluation import recall  # noqa: PLC0415
    from litwatch.quality import replay  # noqa: PLC0415
    from litwatch.store import evaluation  # noqa: PLC0415

    async def work(db: DatabaseGate) -> str | None:
        async with db.read("replay report") as unit:
            return await report(unit)

    async def report(conn: ReadUnit) -> str | None:
        found = eval_run if eval_run is not None else await evaluation.latest(conn, replay.KIND)
        run = await evaluation.get(conn, found) if found is not None else None
        if run is None or run["kind"] != replay.KIND:
            return None
        outcomes = await evaluation.outcomes(conn, found)
        rows = await evaluation.results(conn, found)
        return replay.report(run, recall(outcomes), outcomes, rows)

    text = _run(work, name="eval report")
    if text is None:
        raise SystemExit("no such replay")
    print(text, end="")
    if out is not None:
        out.write_text(text)


@eval_app.command("digests")
def eval_digests(
    since: Since,
    until: Until,
    out: Annotated[Path | None, typer.Option(help="Also write the report here.")] = None,
) -> None:
    """The written digests from --since to --until checked against the voice rules and the
    picks each depth allows, and the Focus papers still due: no calls."""
    from litwatch.core import FAILED_RUNS, Depth  # noqa: PLC0415
    from litwatch.core.digests import DigestPeriod  # noqa: PLC0415
    from litwatch.quality import digests as harness  # noqa: PLC0415
    from litwatch.store import digests as digests_store  # noqa: PLC0415

    first, last = since.date(), until.date()

    async def work(db: DatabaseGate) -> str:
        async with db.read("digests report") as unit:
            return await report(unit)

    async def report(conn: ReadUnit) -> str:
        tree = await taxonomy_store.load(conn)
        topics = tree.shown() if tree else []
        due = {
            t.slug: len(await digests_store.due(conn, tree, t, DigestPeriod.DAY, last, FAILED_RUNS))
            for t in topics
            if t.depth == Depth.FOCUS
        }
        written = await digests_store.written(conn, first, last)
        depths = {t.slug: t.depth.value for t in topics}
        return harness.report(first, last, written, depths, due)

    text = _run(work, name="eval digests")
    print(text, end="")
    if out is not None:
        out.write_text(text)


@eval_app.command("briefs")
def eval_briefs(
    preferences: Annotated[Path, typer.Option(help="The preference set's Briefs.")] = Path(
        "evals/preferences/briefs.yaml"
    ),
    out: Annotated[Path, typer.Option(help="The run's answers, one JSON line a paper.")] = Path(
        "evals/runs/briefs/brief.jsonl"
    ),
    config: Annotated[Path | None, typer.Option(help="Default: ./litwatch.toml.")] = None,
    wait_minutes: Annotated[float, typer.Option(help="How long to wait for a text.")] = 20,
    concurrency: int = 4,
    text: Annotated[
        str,
        typer.Option(
            help="What is sent of the text: pass (as passes send it), as-read, no-appendix."
        ),
    ] = "pass",
) -> None:
    """Write the Brief of each paper of the preference set with the configured `brief` role
    (--config's), reading its text from `serve`'s archive as a full-text pass does. A dry run:
    it reads the papers from the configured database and writes its answers only to `out`, as
    `serve` answers. Its calls are journalled on an evaluation run: production's reuse and price
    estimates leave them out, and only `eval replay`'s estimate reads them. A run resumes where
    it stopped, and so does an item `serve` stopped under, submitted again. `out` names the item
    that last wrote it (`<out>.item`): a run first writes in what that item answered while no
    command followed it, and follows it to its end first while it still runs; then it asks what
    its own arguments still need."""
    from litwatch.quality import briefs, jsonl  # noqa: PLC0415

    command = "eval briefs"
    role = configuration.load(config).models.role(briefs.brief.ROLE)
    if text not in briefs.TEXTS:
        raise SystemExit(f"--text is one of {', '.join(briefs.TEXTS)}")
    path = out.resolve()
    sidecars = [_sidecar(path)]
    _preflight("briefs", command)
    resumed = _caught_up(sidecars, command)
    entries = briefs.load(preferences)
    found = _run(lambda db: _papers(db, [e.id for e in entries]), name=command, migrate=False)
    by_id = {p.id: p for p in found}
    if missing := [e.id for e in entries if e.id not in by_id]:
        console.print(f"[yellow]not in the database, left out: {', '.join(missing)}[/yellow]")

    def again(scope: dict[str, Any]) -> dict[str, Any] | None:
        """The scope with the entries `out` does not answer; None when it answers them all."""
        jsonl.mend(path)
        try:
            todo = briefs.left(
                path,
                briefs.fingerprint(role, text),
                [(by_id[e.id], e) for e in entries if e.id in by_id],
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        return scope | {"entries": [e.model_dump(mode="json") for _, e in todo]} if todo else None

    first = again(
        {
            "out": str(path),
            "role": role.model_dump(mode="json"),
            "tree": seeds.taxonomy().model_dump(mode="json"),
            "concurrency": concurrency,
            "text": text,
            "wait_seconds": wait_minutes * 60,
        }
    )
    if first is None:
        console.print(f"nothing left to ask: {path} holds each answer")
        return
    _submit("briefs", first, command=command, sidecars=sidecars, again=again, resumed=resumed)


@eval_app.command("briefs-report")
def eval_briefs_report(
    title: Annotated[str, typer.Option(help="The report's heading.")] = "Briefs",
    preferences: Annotated[Path, typer.Option(help="The preference set's Briefs.")] = Path(
        "evals/preferences/briefs.yaml"
    ),
    run: Annotated[Path, typer.Option(help="The run's answers.")] = Path(
        "evals/runs/briefs/brief.jsonl"
    ),
) -> None:
    """The papers' abstracts beside their overstatement lines, Briefs and Gists, with the rule
    checks and the owner's judgments, as markdown: no calls."""
    from litwatch.quality import briefs  # noqa: PLC0415
    from litwatch.store import analyses as analyses_store  # noqa: PLC0415

    entries = briefs.load(preferences)
    ids = [e.id for e in entries]

    async def work(db: DatabaseGate) -> tuple[list[Paper], dict[str, str]]:
        async with db.read("briefs report") as unit:
            found = await papers_store.get_many(unit, ids)
            return found, await analyses_store.abstract_gists(unit, ids)

    found, gists = _run(work, name="eval briefs-report", migrate=False)
    papers = {p.id: p for p in found}
    found = briefs.results(run)
    print(briefs.report(title, entries, papers, found, gists, briefs.spent(run)), end="")


@eval_app.command("ladders")
def eval_ladders(
    role: Annotated[list[str] | None, typer.Argument(help="Only these roles.")] = None,
    cases: Annotated[Path, typer.Option(help="The cases, a CSV file.")] = Path(
        "evals/2026-10-19-fallback-ladders/cases.csv"
    ),
    candidates: Annotated[Path, typer.Option(help="TOML: each role's ladder.")] = Path(
        "evals/2026-10-19-fallback-ladders/ladders.toml"
    ),
    out: Annotated[
        Path, typer.Option(help="One <role>.jsonl per role, pending.jsonl and budget.jsonl.")
    ] = Path("evals/2026-10-19-fallback-ladders/runs"),
    ceiling: Annotated[float, typer.Option(help="USD that every run into --out may spend.")] = 25.0,
    which: Annotated[
        str | None, typer.Option("--set", help="Only this set: quality or refusals.")
    ] = None,
    only: Annotated[list[str] | None, typer.Option(help="Only these candidates.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Spend without asking.")] = False,
    concurrency: Annotated[int, typer.Option(min=1)] = 8,
    config: ConfigPath = None,
) -> None:
    """Ask each case's request, as the pipeline builds it, of every candidate on its role's
    ladder (docs/plans/failure-handling.md, P1). A dry run: it reads papers, the taxonomy and
    digests from the configured database and texts the archive already holds, and writes its
    records only to `out`, as `serve` answers; its calls are journalled on an evaluation run, as
    `eval classify`'s are. A run resumes where it stopped. Shows what it would cost and asks
    first, and stops at the ceiling. `serve` builds the requests again with its archive, and
    refuses any that is not the one approved. An item `serve` stopped under is submitted again,
    for what of the approved calls its records do not settle. `out` names the item that last
    wrote it (`item.json`): a run first writes in what that item answered and reserved while no
    command followed it, and follows it to its end first while it still runs; then it asks what
    its own arguments still need."""
    if not (math.isfinite(ceiling) and ceiling > 0):
        raise typer.BadParameter("--ceiling is a positive amount")
    from litwatch.fulltext import archive_for  # noqa: PLC0415
    from litwatch.quality import ladders  # noqa: PLC0415

    cfg = configuration.load(config)
    found = ladders.ladders(candidates)
    if which is not None and which not in ladders.SETS:
        raise typer.BadParameter(f"--set is one of {', '.join(ladders.SETS)}")
    if unknown := set(role or []) - set(found):
        raise typer.BadParameter(f"no ladder for {', '.join(sorted(unknown))}")
    names = {c.name for ladder in found.values() for c in ladder.candidates}
    if unknown := set(only or []) - names:
        raise typer.BadParameter(
            f"no candidate {', '.join(sorted(unknown))}; the candidates: {', '.join(sorted(names))}"
        )
    chosen = [
        c
        for c in ladders.cases(cases)
        if c.role in (role or found) and (which is None or c.set == which)
    ]
    archive = archive_for(cfg)

    async def prepare(db: DatabaseGate) -> dict[str, Any]:
        try:
            return await ladders.requests(db, archive, chosen)
        finally:
            if archive is not None:
                await archive.aclose()

    command = "eval ladders"
    _preflight("ladders", command)
    built = _run(prepare, name=command, migrate=False)
    for key, why in built.items():
        if isinstance(why, str):
            console.print(f"[yellow]left out: {key}: {why}[/yellow]")
    path = out.resolve()
    try:
        # Held while `serve` makes the calls and this command writes their records.
        with ladders.locked(path):
            sidecars = [_sidecar(path, directory=True)]
            resumed = _caught_up(sidecars, command)
            done = ladders.recorded(path)
            work = ladders.todo(chosen, found, built, done, only)
            by = Counter((t.case.role, t.candidate.name) for t in work)
            for (r, name), n in sorted(by.items()):
                cost = sum(ladders.estimate(t.ladder, t.candidate, t.request) for t in work
                           if (t.case.role, t.candidate.name) == (r, name))  # fmt: skip
                console.print(f"{r} {name}: {n} calls, estimated ${cost:.2f}")
            expected = sum(ladders.estimate(t.ladder, t.candidate, t.request) for t in work)
            spent = ladders.spent(done, ladders.reservations(path))
            console.print(
                f"{len(work)} calls, estimated ${expected:.2f}; spent so far "
                f"${spent:.2f} of the ${ceiling:.2f} ceiling"
            )
            if not work or (not yes and not typer.confirm("Spend it?")):
                return

            def calls(these: list[Any], spent: float) -> dict[str, Any]:
                # Each call with the fingerprint of its request, as estimated and approved.
                todo = [(t.case.key, t.candidate.name, ladders.asked(t.candidate, t.request))
                        for t in these]  # fmt: skip
                return {"todo": todo, "spent": spent}

            def again(scope: dict[str, Any]) -> dict[str, Any] | None:
                """The approved calls the records do not settle, and what the records spent."""
                done = ladders.recorded(path)
                left = ladders.left(work, done)
                spent = ladders.spent(done, ladders.reservations(path))
                return scope | calls(left, spent) if left else None

            scope = {
                "out": str(path),
                "cases": [c.model_dump(mode="json") for c in chosen],
                "ladders": {r: x.model_dump(mode="json") for r, x in found.items()},
                "ceiling": ceiling,
                "concurrency": concurrency,
            } | calls(work, spent)
            _submit(
                "ladders", scope, command=command, sidecars=sidecars, again=again, resumed=resumed
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


@eval_app.command("ladders-report")
def eval_ladders_report(
    cases: Annotated[Path, typer.Option(help="The cases, a CSV file.")] = Path(
        "evals/2026-10-19-fallback-ladders/cases.csv"
    ),
    candidates: Annotated[Path, typer.Option(help="TOML: each role's ladder.")] = Path(
        "evals/2026-10-19-fallback-ladders/ladders.toml"
    ),
    out: Annotated[Path, typer.Option(help="Where the runs are.")] = Path(
        "evals/2026-10-19-fallback-ladders/runs"
    ),
    config: ConfigPath = None,
) -> None:
    """The runs' tables per role and set, then every answer on the refusals set's papers that a
    candidate refused or answered unusably, as markdown: no model calls. It builds each case's
    request as `eval ladders` does and reads only the calls to it; titles are read from the
    database, else the archive's catalog."""
    from litwatch.fulltext import archive_for  # noqa: PLC0415
    from litwatch.quality import ladders  # noqa: PLC0415

    found, chosen = ladders.ladders(candidates), ladders.cases(cases)
    archive = archive_for(configuration.load(config))

    async def work(db: DatabaseGate) -> tuple[dict[str, Any], dict[str, str]]:
        ids = sorted({c.paper for c in chosen if c.paper and c.set == "refusals"})
        try:
            built = await ladders.requests(db, archive, chosen)
            found_papers = await ladders.papers(db, archive, ids)
            return built, {p.id: p.title for p in found_papers.values()}
        finally:
            if archive is not None:
                await archive.aclose()

    built, titles = _run(work, name="eval ladders-report", migrate=False)
    calls = ladders.rows(out)
    print(ladders.tables(found, chosen, calls, built, ladders.reservations(out)), end="")
    print("\n### Answers on the refusals set\n")
    print(ladders.answers(found, chosen, calls, built, titles), end="")


async def _papers(db: DatabaseGate, ids: list[str]) -> list[Paper]:
    """The papers of `ids` the database holds, for an evaluation's dry run."""
    async with db.read("evaluation papers") as unit:
        return await papers_store.get_many(unit, ids)


# -- library ---------------------------------------------------------------------------------------


@library_app.command("sync")
def library_sync(config: ConfigPath = None) -> None:
    """Settle every paper with a verdict, a Weight or the owner's word: effective grades,
    rarity, the library, and their events. For a database written around the settle."""
    rules = word.rules(configuration.load(config))
    console.print_json(data=_run(lambda db: word.sync(db, rules=rules), name="library sync"))


@app.command()
def publications(
    paper: Annotated[
        str | None, typer.Option(help="Look up this one paper, whether saved or not.")
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            help="At most this many library papers, per work item: one submitted again after "
            "`serve` stops counts afresh."
        ),
    ] = 200,
) -> None:
    """Look up published versions on DBLP for the library papers that are due, as the
    scheduler does daily, or for one paper. `serve` runs it."""
    if paper:
        _submit("publications", {"paper": paper}, command="publications")
        return
    # Admitted as the scheduled lookups are: a run here and a scheduled one never overlap.
    admission = commands.ADMISSIONS["publications"]
    _submit("publications", {"limit": limit}, command="publications", admission=admission)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8080,
    metrics_port: Annotated[
        int | None,
        typer.Option(
            envvar="LITWATCH_METRICS_PORT",
            min=1,
            max=65535,
            help="Also serve /metrics and /status, without a token, on this port of --host.",
        ),
    ] = None,
    config: ConfigPath = None,
) -> None:
    """Serve the API and the web app, with the integrations the
    configuration enables. Needs LITWATCH_API_TOKEN; LITWATCH_READ_TOKEN, if set, is a
    second token that may only search and read papers (for the deep service);
    LITWATCH_MCP_TOKEN, if set, serves the MCP server at /mcp, for research agents, behind
    that token alone."""
    from litwatch.api import create_app  # noqa: PLC0415

    token = configuration.secret("LITWATCH_API_TOKEN")
    if not token:
        raise SystemExit("LITWATCH_API_TOKEN (or LITWATCH_API_TOKEN_FILE) is not set")
    from litwatch import integrations  # noqa: PLC0415

    cfg = configuration.load(config)
    try:
        loaded = integrations.load(cfg)
    except integrations.IntegrationError as exc:
        raise SystemExit(str(exc)) from exc
    from litwatch import fulltext  # noqa: PLC0415

    # The process's one gate: migrations, then the app and the monitor beside it, which read
    # it until both servers have returned. The app does not open or close a gate it is given.
    db = DatabaseGate(configuration.database_url())
    try:
        app_ = create_app(
            configuration.database_url(),
            token,
            cfg,
            integrations=loaded,
            read_token=configuration.secret("LITWATCH_READ_TOKEN"),
            archive=fulltext.archive_for(cfg),
            mcp_token=configuration.secret("LITWATCH_MCP_TOKEN"),
            db=db,
        )
    except ValueError as exc:  # the MCP token reuses another
        raise SystemExit(str(exc)) from exc

    async def serving(db: DatabaseGate) -> None:
        await _serve(app_, host, port, metrics_port)

    # `serve` attributes its own calls: its startup and stop, each request, each item.
    _run(serving, name="serve", db=db, attribute=False)


async def _serve(app_: Any, host: str, port: int, metrics_port: int | None) -> None:
    """The app, and the monitoring app beside it on `metrics_port`. The app's server owns the
    signals; when either server stops, for a signal or a failure, the other stops too, and a
    failure is raised."""
    import uvicorn  # noqa: PLC0415

    from litwatch.api.monitor import create_monitor  # noqa: PLC0415

    class _Beside(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            yield

    main = uvicorn.Server(uvicorn.Config(app_, host=host, port=port, log_config=None))
    if metrics_port is None:
        await main.serve()
        return
    beside = _Beside(
        uvicorn.Config(
            create_monitor(app_), host=host, port=metrics_port, log_config=None, lifespan="off"
        )
    )
    tasks = [asyncio.create_task(main.serve()), asyncio.create_task(beside.serve())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    main.should_exit = beside.should_exit = True
    await asyncio.gather(*tasks)
    if not main.started or not beside.started:
        raise SystemExit("a server could not start")


@app.command("openapi")
def openapi(out: Annotated[Path, typer.Argument(help="Where to write the schema.")]) -> None:
    """Write the API's OpenAPI schema, which the web app's client is generated from."""
    import json  # noqa: PLC0415

    from litwatch.api import create_app  # noqa: PLC0415

    schema = create_app("postgresql://unused", "unused").openapi()
    out.write_text(json.dumps(schema, indent=2) + "\n")
    console.print(f"wrote {out}")


@app.command("fulltext")
def fulltext_queue(
    limit: Annotated[
        int | None,
        typer.Option(
            help="At most this many passes written or failed, per work item: one submitted "
            "again after `serve` stops counts afresh."
        ),
    ] = None,
    wait: Annotated[
        int, typer.Option(help="Keep trying, for at most this many seconds, while some await text.")
    ] = 0,
    deep: Annotated[
        bool, typer.Option(help="Work the Deep card passes instead, with the deep integration.")
    ] = False,
) -> None:
    """Work the full-text queue as the service's workers do: try every pass that is due once,
    and with --wait again every half minute while some still await their text from the archive.
    Text passes by default; Deep card passes with --deep, one at a time, each a paid job on the
    deep service. `serve` runs it, with its archive and deep integration."""
    scope = {"limit": limit, "wait": wait}
    if deep:
        # Admitted as `serve`'s Deep passes are, so one Deep job runs at a time between them.
        _submit("deep", scope, command="fulltext", admission=commands.ADMISSIONS["deep"])
        return
    _submit("fulltext", scope, command="fulltext")


@app.command()
def ping(role: Annotated[str, typer.Argument(help=", ".join(configuration.ROLES))]) -> None:
    """Send one small request through a role of `serve`'s, to check its key, model and price."""
    if role not in configuration.ROLES:
        raise typer.BadParameter(f"no role {role}; the roles: {', '.join(configuration.ROLES)}")
    _submit("ping", {"role": role}, command="ping")


if __name__ == "__main__":
    app()
