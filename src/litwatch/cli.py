"""The jobs and a few queries from a terminal."""

from __future__ import annotations

import asyncio
import contextlib
import math
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
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
from litwatch.gates.database import BusyError, DatabaseGate, PoolTimeoutError, ReadUnit
from litwatch.gates.work import LEASE, WorkGate
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
    migrate: bool = True,
    db: DatabaseGate | None = None,
    attribute: bool = True,
) -> T:
    """Run `work` with the command's Database gate, the process's one, on the configured
    database, migrated first: `db` when the command built it already (unopened), else a new
    one. The gate is open for the run and closed at its end. Its gate calls are the command's
    request (`attribute`): `serve` alone attributes its own, per request and item."""
    logs.setup()

    async def main() -> T:
        async with db or DatabaseGate(configuration.database_url()) as gate:
            if migrate:
                await _migrate(gate)
            return await work(gate)

    try:
        with attributed(_request()) if attribute else contextlib.nullcontext():
            return asyncio.run(main())
    except (store.LegacyDatabaseError, BusyError, PoolTimeoutError) as exc:
        raise SystemExit(str(exc)) from exc


def _request() -> Attribution:
    """The running command as a request: `cli litwatch eval report`, say."""
    ctx = click.get_current_context(silent=True)
    return Attribution.request(f"cli {ctx.command_path if ctx else 'litwatch'}")


# -- commands `serve` runs: submitted, then followed --------------------------------------------

# How often a command following its item looks at it when no notification comes.
FOLLOW_SECONDS = 2.0


def _submit(
    name: str,
    scope: dict[str, Any],
    *,
    admission: str | None = None,
    migrate: bool = False,
    here: Callable[[DatabaseGate], commands.Serving] | None = None,
) -> None:
    """Submit the command `name`'s work item (`jobs.commands`) with `scope`, admitted by
    `admission` (by default its own key), and follow it to its end (`_follow`): the database is
    the one `serve` works from, and `serve` runs it. Refuses when no `serve` runs its kind (a
    live `workers` row): it never runs the work itself, but with `here` (what to run it with, on
    the command's gate), on a Work gate of its own that serves this item alone, for a database
    no `serve` works from. Ctrl-C asks whether to cancel the item or leave it to run
    (`_interrupted`); with `here` it is cancelled. A failed item fails the command with its
    error."""
    logs.setup()
    work_kind, key = commands.kind(name), str(uuid.uuid4())

    async def main() -> WorkItem:
        async with DatabaseGate(configuration.database_url()) as db:
            if migrate:
                await _migrate(db)
            if here is None:
                async with db.read("is it served") as unit:
                    served = await queue.serving(unit, work_kind, LEASE)
                if not served:
                    raise SystemExit(
                        f"no `litwatch serve` runs {work_kind} on this database: start one "
                        "(docs/agents/dev-host.md); a command never runs its work itself"
                    )
            async with db.write(f"{name}: submit") as unit:
                item = await queue.submit(
                    unit, work_kind, key, scope=scope, admission=admission or key
                )
            console.print(f"item {item.id}: {work_kind} (Ctrl-C to cancel it or leave it running)")
            if here is None:
                return await _follow(db, item.id)
            alone = {work_kind: commands.CommandWorker(here(db))}
            running = asyncio.create_task(WorkGate(db).run(alone, keys={work_kind: [key]}))
            try:
                return await _follow(db, item.id)
            finally:
                running.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await running

    try:
        with attributed(_request()):
            ended = asyncio.run(main())
    except KeyboardInterrupt:
        _interrupted(work_kind, key, cancel=here is not None)
    except (store.LegacyDatabaseError, PoolTimeoutError) as exc:
        raise SystemExit(str(exc)) from exc
    _ended(ended)


def _ended(item: WorkItem) -> None:
    if item.outcome == Outcome.FAILED:
        raise SystemExit(f"item {item.id} failed: {item.error}")


async def _follow(db: DatabaseGate, item_id: int) -> WorkItem:
    """Follow the item `item_id` to its end, showing its results as they arrive (`_Shown`):
    woken by a notification (its results', or any item's settling) and else every
    `FOLLOW_SECONDS`. It only reads: the item is `serve`'s, which may stop and start again
    meanwhile (the item goes back to pending and runs again). It says so when no `serve` runs
    the item's kind while the item waits. Returns the item, ended."""
    shown, wake = _Shown(), asyncio.Event()
    listening = asyncio.create_task(_woken(db, wake))
    last, unserved = 0, False
    was: ItemState | None = None
    try:
        while True:
            wake.clear()
            async with db.read("follow") as unit:
                item = await queue.get(unit, item_id)
                if item is None:
                    raise SystemExit(f"no work item {item_id}")
                found = await queue.results(unit, item_id, last)
                served = await queue.serving(unit, item.kind, LEASE)
            for last, body in found:  # noqa: B007
                shown.show(body)
            if item.state == ItemState.ENDED:
                return item
            if item.state != was:
                if item.state == ItemState.RUNNING and item.attempts > 1:
                    console.print(f"item {item_id} runs again (attempt {item.attempts})")
                elif item.state == ItemState.PENDING and was == ItemState.RUNNING:
                    console.print(f"item {item_id} waits to run again")
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
    lines appended to their files once each, whoever followed the item before (a line the file
    holds is not written again). A record cut off at a file's end, by a kill mid-write, is cut
    before the first line is added."""

    def __init__(self) -> None:
        self.files: dict[str, set[str]] = {}

    def show(self, body: dict[str, Any]) -> None:
        if "say" in body:
            console.print(body["say"], markup=False)
        elif "data" in body:
            console.print_json(data=body["data"])
        elif "to" in body:
            self.line(Path(body["to"]), body["line"])

    def line(self, path: Path, line: str) -> None:
        held = self.files.get(str(path))
        if held is None:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            if text and not text.endswith("\n"):
                text = text[: text.rfind("\n") + 1]
                path.write_text(text, encoding="utf-8")
            held = self.files[str(path)] = set(text.splitlines())
        if line in held:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        held.add(line)


def _interrupted(work_kind: str, key: str, *, cancel: bool = False) -> NoReturn:
    """Ctrl-C while a command follows its item: ask whether to cancel the item (`cancel`: do,
    without asking) or leave it to run, which `litwatch work follow` picks back up. Its
    submission may or may not have committed: the item is found by its key."""

    async def find(db: DatabaseGate) -> WorkItem | None:
        async with db.read("interrupted: the item") as unit:
            return await queue.find(unit, work_kind, key)

    item = _run(find, migrate=False)
    if item is None:
        raise SystemExit("stopped before its work item was submitted")
    _stop(item, cancel=cancel)


def _stop(item: WorkItem, *, cancel: bool) -> NoReturn:
    if item.state != ItemState.ENDED and not cancel:
        try:
            cancel = typer.confirm(
                f"\nCancel item {item.id}? Otherwise it goes on, and "
                f"`litwatch work follow {item.id}` picks it back up",
                default=False,
            )
        except click.Abort:
            cancel = False
    if item.state != ItemState.ENDED and cancel:

        async def cancelled(db: DatabaseGate) -> WorkItem | None:
            async with db.write("cancel the item") as unit:
                return await queue.cancel(unit, item.id, "cancelled by the owner (Ctrl-C)")

        item = _run(cancelled, migrate=False) or item
    if item.state == ItemState.ENDED:
        raise SystemExit(f"item {item.id} ended {item.outcome}: {item.error or 'done'}")
    console.print(f"item {item.id} goes on: `litwatch work follow {item.id}` picks it back up")
    raise typer.Exit(130)


@work_app.command("follow")
def work_follow(item_id: Annotated[int, typer.Argument(help="The work item's id.")]) -> None:
    """Follow a command's work item to its end, as the command did before Ctrl-C left it to
    run: its output from the start, its files' lines written once each."""
    logs.setup()

    async def main() -> WorkItem:
        async with DatabaseGate(configuration.database_url()) as db:
            return await _follow(db, item_id)

    try:
        with attributed(_request()):
            ended = asyncio.run(main())
    except KeyboardInterrupt:

        async def found(db: DatabaseGate) -> WorkItem | None:
            async with db.read("interrupted: the item") as unit:
                return await queue.get(unit, item_id)

        item = _run(found, migrate=False)
        assert item is not None  # followed until now
        _stop(item, cancel=False)
    _ended(ended)


async def _migrate(db: DatabaseGate) -> list[str]:
    # On the migration lock's own session, so the migrations cannot outlive the lock.
    with attributed(Attribution.system("startup: migrations")):
        async with db.startup("migration") as conn:
            return await store.migrate(conn)


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending migrations."""
    applied = _run(_migrate, migrate=False)
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
        _submit("intake", {"catalog_from": catalog_from.date().isoformat()})
        return
    _submit("intake", {"days": days})


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
    scope = {"first": first.isoformat(), "last": last.isoformat()}
    if not here:
        _submit("intake", scope)
        return
    from litwatch.llm import ModelGate  # noqa: PLC0415

    cfg = configuration.load(config)

    def serving(db: DatabaseGate) -> commands.Serving:
        # No model is called: a Model gate of no providers refuses any call.
        return commands.Serving(db, cfg, ModelGate(configuration.Models(), {}))

    _submit("intake", scope, migrate=True, here=serving)


@app.command()
def nightly(
    fetch: Annotated[bool, typer.Option(help="Harvest arXiv first.")] = True,
    days: Annotated[int | None, typer.Option(help="Look back this many days.")] = None,
    limit: Annotated[
        int | None, typer.Option(help="At most this many papers per step (a trial run).")
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
    _submit("nightly", {"fetch": fetch, "days": days, "limit": limit, "digests": digests})


# -- digests --------------------------------------------------------------------------------------


@digests_app.command("write")
def digests_write(since: Since, until: Until) -> None:
    """Write the digests of every day from --since to --until, and the weekly digests of the
    weeks inside it, as the nightly run would have on those days, over the papers due now
    (the restart, a missed night). Published digests stay as they are. `serve` runs it."""
    _submit("digests", {"since": since.date().isoformat(), "until": until.date().isoformat()})


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

    found, chosen, trace, title, notes, reviews = _run(work, migrate=False)
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

    found = _run(work, migrate=False)
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

    saved = _run(work)
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

    tree = _run(work)
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
    titles and abstracts from the configured database and writes only to `out`, as `serve`
    answers; a run resumes where it stopped, asking only what `out` does not hold."""
    import tomllib  # noqa: PLC0415

    from litwatch.config import Models, RoleConfig  # noqa: PLC0415
    from litwatch.quality import classify  # noqa: PLC0415

    roles = tomllib.loads(candidates.read_text())
    if unknown := set(candidate) - set(roles):
        raise typer.BadParameter(f"no candidate {', '.join(sorted(unknown))} in {candidates}")
    tree, evaluation = seeds.taxonomy(), classify.load(labels)
    ids = evaluation.papers[:limit]
    found = {p.id for p in _run(lambda db: _papers(db, ids), migrate=False)}
    if missing := len(ids) - len(found):
        console.print(f"[yellow]{missing} papers are not in the database and are left out[/yellow]")
    chosen = []
    for name in candidate:
        role = RoleConfig.model_validate(roles[name])
        path = (out / f"{name}.jsonl").resolve()
        asked = classify.fingerprint(Models(classify=role), tree)
        try:
            todo = classify.left(path, asked, [i for i in ids if i in found])
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        chosen.append(
            {"name": name, "role": role.model_dump(mode="json"), "papers": todo, "out": str(path)}
        )
    scope = {"tree": tree.model_dump(mode="json"), "concurrency": concurrency}
    _submit("classify", scope | {"candidates": chosen})


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
    must be the one estimated with (--config); a run cut short goes on with --resume."""
    from litwatch.quality import replay  # noqa: PLC0415

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
        r, cost = _run(prepare)
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

    async def start(db: DatabaseGate) -> int:
        params = {"cohorts": cohort, "negatives": sample, "seed": replay.SEED, "limit": limit}
        async with db.write("replay: start") as unit:
            return await replay.start(unit, cfg, r, params, note)

    if r.eval_run_id is None:
        r.eval_run_id = _run(start, migrate=False)
        console.print(f"replay {r.eval_run_id}")
    _submit("replay", {"eval_run_id": r.eval_run_id})


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

    text = _run(work)
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

    text = _run(work)
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
    it reads the papers from the configured database and writes only to `out`, as `serve`
    answers; a run resumes where it stopped."""
    from litwatch.quality import briefs  # noqa: PLC0415

    role = configuration.load(config).models.role(briefs.brief.ROLE)
    if text not in briefs.TEXTS:
        raise SystemExit(f"--text is one of {', '.join(briefs.TEXTS)}")
    entries = briefs.load(preferences)
    found = _run(lambda db: _papers(db, [e.id for e in entries]), migrate=False)
    by_id = {p.id: p for p in found}
    if missing := [e.id for e in entries if e.id not in by_id]:
        console.print(f"[yellow]not in the database, left out: {', '.join(missing)}[/yellow]")
    path = out.resolve()
    try:
        todo = briefs.left(
            path,
            briefs.fingerprint(role, text),
            [(by_id[e.id], e) for e in entries if e.id in by_id],
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _submit(
        "briefs",
        {
            "out": str(path),
            "entries": [e.model_dump(mode="json") for _, e in todo],
            "role": role.model_dump(mode="json"),
            "tree": seeds.taxonomy().model_dump(mode="json"),
            "concurrency": concurrency,
            "text": text,
            "wait_seconds": wait_minutes * 60,
        },
    )


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

    found, gists = _run(work, migrate=False)
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
    digests from the configured database and texts the archive already holds, and writes only to
    `out`, as `serve` answers; a run resumes where it stopped. Shows what it would cost and asks
    first, and stops at the ceiling. `serve` builds the requests again with its archive."""
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

    built = _run(prepare, migrate=False)
    for key, why in built.items():
        if isinstance(why, str):
            console.print(f"[yellow]left out: {key}: {why}[/yellow]")
    path = out.resolve()
    try:
        # Held while `serve` makes the calls and this command writes their records.
        with ladders.held(path) as done:
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
            scope = {
                "out": str(path),
                "cases": [c.model_dump(mode="json") for c in chosen],
                "ladders": {r: x.model_dump(mode="json") for r, x in found.items()},
                "todo": [(t.case.key, t.candidate.name) for t in work],
                "ceiling": ceiling,
                "spent": spent,
                "concurrency": concurrency,
            }
            _submit("ladders", scope)
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

    built, titles = _run(work, migrate=False)
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
    console.print_json(data=_run(lambda db: word.sync(db, rules=rules)))


@app.command()
def publications(
    paper: Annotated[
        str | None, typer.Option(help="Look up this one paper, whether saved or not.")
    ] = None,
    limit: Annotated[int, typer.Option(help="At most this many library papers.")] = 200,
) -> None:
    """Look up published versions on DBLP for the library papers that are due, as the
    scheduler does daily, or for one paper. `serve` runs it."""
    if paper:
        _submit("publications", {"paper": paper})
        return
    # Admitted as the scheduled lookups are: a run here and a scheduled one never overlap.
    _submit("publications", {"limit": limit}, admission=commands.ADMISSIONS["publications"])


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

    _run(serving, db=db)


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
        int | None, typer.Option(help="At most this many passes written or failed.")
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
        _submit("deep", scope, admission=commands.ADMISSIONS["deep"])
        return
    _submit("fulltext", scope)


@app.command()
def ping(role: Annotated[str, typer.Argument(help=", ".join(configuration.ROLES))]) -> None:
    """Send one small request through a role of `serve`'s, to check its key, model and price."""
    if role not in configuration.ROLES:
        raise typer.BadParameter(f"no role {role}; the roles: {', '.join(configuration.ROLES)}")
    _submit("ping", {"role": role})


if __name__ == "__main__":
    app()
