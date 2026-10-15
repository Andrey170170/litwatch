"""The jobs and a few queries from a terminal."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import psycopg
import typer
import yaml
from rich.console import Console

from litwatch import config as configuration
from litwatch import feedback, seeds, store
from litwatch.archive import ArchiveError
from litwatch.core import Topic
from litwatch.feedback import carry, seed, word
from litwatch.integrations import IntegrationError
from litwatch.integrations import archive as archive_integration
from litwatch.jobs import digest as digest_job
from litwatch.jobs import intake as intake_job
from litwatch.jobs import migrate as migrate_job
from litwatch.jobs import nightly as nightly_job
from litwatch.jobs import publications as lookups
from litwatch.jobs import reviews as review_jobs
from litwatch.jobs.context import model_run
from litwatch.jobs.lock import run_lock
from litwatch.publications import Dblp
from litwatch.store import digest as digest_store
from litwatch.store import legacy as legacy_store
from litwatch.store import papers as papers_store
from litwatch.store import runs
from litwatch.store import topics as topics_store
from litwatch.store import triage as triage_store

app = typer.Typer(no_args_is_help=True, add_completion=False, help="litwatch")
db_app = typer.Typer(no_args_is_help=True, help="Database migrations.")
topics_app = typer.Typer(no_args_is_help=True, help="Topics as YAML, in and out.")
eval_app = typer.Typer(no_args_is_help=True, help="Dry runs measured against the owner's labels.")
library_app = typer.Typer(no_args_is_help=True, help="Effective grades and the library.")
issue_app = typer.Typer(no_args_is_help=True, help="The daily issue: build it, read it.")
taxonomy_app = typer.Typer(no_args_is_help=True, help="The class tree and its followed topics.")
intake_app = typer.Typer(help="Take papers in: from arXiv, or a window from the archive.")
app.add_typer(db_app, name="db")
app.add_typer(topics_app, name="topics")
app.add_typer(eval_app, name="eval")
app.add_typer(library_app, name="library")
app.add_typer(issue_app, name="issue")
app.add_typer(taxonomy_app, name="taxonomy")
app.add_typer(intake_app, name="intake")
console = Console()

ConfigPath = Annotated[
    Path | None, typer.Option("--config", help="litwatch.toml; default $LITWATCH_CONFIG or ./")
]


def _run[T](work: Callable[[psycopg.AsyncConnection], Awaitable[T]], *, migrate: bool = True) -> T:
    """Run `work` on a connection to the configured database, migrated first."""
    configuration.load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    async def main() -> T:
        async with await store.connect(configuration.database_url()) as conn:
            if migrate:
                await store.migrate(conn)
            return await work(conn)

    try:
        return asyncio.run(main())
    except store.LegacyDatabaseError as exc:
        raise SystemExit(str(exc)) from exc


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending migrations."""
    applied = _run(store.migrate, migrate=False)
    console.print("applied: " + (", ".join(applied) or "nothing, already up to date"))


@db_app.command("adopt")
def db_adopt(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation.")] = False,
) -> None:
    """Turn a COPY of a 0.x database into a v1 one. 0.x cannot use the database afterwards."""
    if not yes:
        typer.confirm("This renames 0.x tables. Is this database a copy?", abort=True)
    applied = _run(store.adopt_0x, migrate=False)
    console.print("adopted; applied: " + ", ".join(applied))


@db_app.command("migrate-0x")
def db_migrate_0x(
    leave_behind: Annotated[
        bool, typer.Option(help="Migrate even though some 0.x selections have no v1 topic.")
    ] = False,
    config: ConfigPath = None,
) -> None:
    """Carry 0.14's verdicts into v1, once, after `db adopt` and `topics import`: Watch tiers
    become migrated verdicts with a take for each positive, Radar and Pulse tiers 1 to 3 become
    memberships, every stored paper counts as triaged. Nothing is announced in an issue.
    Running it again adds nothing."""
    cfg = configuration.load(config)
    try:
        console.print_json(
            data=_run(lambda conn: migrate_job.from_0x(conn, cfg, leave_behind=leave_behind))
        )
    except migrate_job.MigrationError as exc:
        raise SystemExit(str(exc)) from exc


@db_app.command("import-feedback")
def db_import_feedback(
    source: Annotated[
        str, typer.Option("--source", help="DSN of the v1 database the feedback was given in.")
    ],
) -> None:
    """Copy the owner's feedback from another v1 database into this one: labels (not
    provisional), reactions, appraisals, take ratings, Calibrate sessions, saves and unsaves,
    and reported misses, for papers this database holds, with their original times. The
    source is only read. Running it again adds nothing. It does NOT carry topics: use
    `litwatch topics export` there and `litwatch topics import` here first."""

    async def work(conn: psycopg.AsyncConnection) -> dict[str, int]:
        async with await store.connect(source) as other:
            await other.execute("SET default_transaction_read_only = on")
            return await carry.import_feedback(conn, other)

    try:
        console.print_json(data=_run(work))
    except carry.SourceError as exc:
        raise SystemExit(str(exc)) from exc


@intake_app.callback(invoke_without_command=True)
def intake(
    ctx: typer.Context,
    days: Annotated[int | None, typer.Option(help="Look back this many days.")] = None,
    config: ConfigPath = None,
) -> None:
    """Fetch new and revised papers for the configured categories from arXiv."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = configuration.load(config)

    async def work(conn: psycopg.AsyncConnection) -> Any:
        async with run_lock(conn):
            return await intake_job.run(conn, cfg, days)

    result = _run(work)
    console.print(
        f"intake {result.details['status']}: {result.fetched} papers, "
        f"{result.inserted} new, {result.updated} updated"
    )


@intake_app.command("archive")
def intake_archive(
    since: Annotated[datetime | None, typer.Option(formats=["%Y-%m-%d"], help="First day.")] = None,
    until: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Last day; default today (UTC).")
    ] = None,
    days: Annotated[int | None, typer.Option(help="Instead of --since: this many days.")] = None,
    config: ConfigPath = None,
) -> None:
    """Read the papers first submitted in a window, in the configured categories, from the
    archive ($LITWATCH_ARCHIVE_URL and $LITWATCH_ARCHIVE_TOKEN, or the archive integration)."""
    if (since is None) == (days is None) or (days is not None and days < 1):
        raise typer.BadParameter("give --since or a positive --days")
    cfg = configuration.load(config)
    last = until.date() if until else datetime.now(UTC).date()
    first = since.date() if since else last - timedelta(days=days - 1)
    if first > last:
        raise typer.BadParameter("--since must not follow --until")

    async def work(conn: psycopg.AsyncConnection) -> Any:
        client = archive_integration.source(cfg)
        try:
            async with run_lock(conn):
                return await intake_job.from_archive(conn, cfg, client, first, last)
        finally:
            await client.aclose()

    try:
        result = _run(work)
    except (ArchiveError, IntegrationError) as exc:
        raise SystemExit(f"archive: {exc}") from exc
    console.print(
        f"intake from the archive, {first} to {last}: {result.fetched} papers, "
        f"{result.inserted} new, {result.updated} updated"
        + (f", {len(result.details['skipped'])} without text" if result.details["skipped"] else "")
    )


@app.command()
def nightly(
    fetch: Annotated[bool, typer.Option(help="Harvest arXiv first.")] = True,
    days: Annotated[int | None, typer.Option(help="Look back this many days.")] = None,
    limit: Annotated[int | None, typer.Option(help="Triage at most this many papers.")] = None,
    config: ConfigPath = None,
) -> None:
    """Intake, triage with the keyword audit, and grading, as the scheduler runs them."""
    cfg = configuration.load(config)
    run_id = _run(lambda conn: nightly_job.run(conn, cfg, fetch=fetch, days=days, limit=limit))
    run = _run(lambda conn: runs.get(conn, run_id))
    console.print(f"run {run_id}: cost ${run['cost']}")
    console.print_json(data=run["counts"])


@app.command("mark-triaged")
def mark_triaged() -> None:
    """Count every stored paper as triaged, so the next run starts from new papers only."""
    console.print(f"{_run(triage_store.mark_migrated)} papers marked")


# -- topics ----------------------------------------------------------------------------------------


@topics_app.command("list")
def topics_list() -> None:
    for topic in _run(lambda conn: topics_store.all(conn, None)):
        console.print(
            f"{topic.kind.value:6} {topic.status.value:7} r{topic.revision}  {topic.slug}"
            f"  ({len(topic.keywords)} keywords)"
        )


async def _save_all(conn: psycopg.AsyncConnection, topics: list[Topic]) -> list[Topic]:
    """Save the topics; one whose kind or model-visible content changed has its papers
    settled again (a topic no longer Watch drops its grades; takes bound to the old rubric
    become stale)."""
    saved = []
    for topic in topics:
        # The kind is read under the row lock the save takes, so a concurrent import cannot
        # make this one miss the transition.
        async with conn.transaction():
            before = await topics_store.kind_for_update(conn, topic.slug)
            revision = (await topics_store.get(conn, topic.slug) or topic).revision
            saved.append(await topics_store.save(conn, topic))
            if before is not None and (before != topic.kind or saved[-1].revision != revision):
                await word.settle_topic(conn, topic.slug)
    return saved


@topics_app.command("import")
def topics_import(
    path: Annotated[Path | None, typer.Argument(help="A YAML file or a directory of them.")] = None,
    starter: Annotated[bool, typer.Option(help="The topics shipped with litwatch.")] = False,
    legacy: Annotated[
        bool, typer.Option(help="The selections of an adopted 0.x database.")
    ] = False,
) -> None:
    """Create or update topics. A changed topic gets a new revision and applies forward."""
    if sum((path is not None, starter, legacy)) != 1:
        raise typer.BadParameter("give a path, --starter or --legacy")

    async def work(conn: psycopg.AsyncConnection) -> list[Topic]:
        if legacy:
            return await _save_all(conn, await legacy_store.topics(conn))
        return await _save_all(conn, seeds.starter() if starter else seeds.load(path))

    for topic in _run(work):
        console.print(f"{topic.slug}: {topic.kind.value}, revision {topic.revision}")


@topics_app.command("export")
def topics_export(directory: Path) -> None:
    """Write every topic to `<directory>/<slug>.yaml`."""
    directory.mkdir(parents=True, exist_ok=True)
    for topic in _run(lambda conn: topics_store.all(conn, None)):
        (directory / f"{topic.slug}.yaml").write_text(seeds.dump(topic))
        console.print(f"wrote {topic.slug}.yaml")


@taxonomy_app.command("check")
def taxonomy_check(
    path: Annotated[Path | None, typer.Argument(help="A taxonomy YAML; default the seed.")] = None,
) -> None:
    """Validate a taxonomy file and summarise it."""
    try:
        tree = seeds.taxonomy(path)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    levels = Counter(tree.level(c.slug) for c in tree.classes)
    console.print(
        f"{len(tree.classes)} classes ("
        + ", ".join(f"{n} at level {level}" for level, n in sorted(levels.items()))
        + ")"
    )
    for topic in tree.topics:
        console.print(f"{topic.depth.value:6} {topic.monogram}  {tree.get(topic.class_).name}")


# -- evaluation ------------------------------------------------------------------------------------


@eval_app.command("seed")
def eval_seed(
    labels: Annotated[
        Path | None, typer.Option(help="The 0.x label file; gold entries become labels.")
    ] = None,
    legacy: Annotated[
        bool, typer.Option(help="0.x verdicts: provisional positives and the regression set.")
    ] = False,
) -> None:
    """Bring in what 0.x left behind as starting material for the evaluation."""

    async def work(conn: psycopg.AsyncConnection) -> dict[str, int]:
        counts: dict[str, int] = {}
        if labels:
            counts |= await seed.from_label_file(conn, labels)
        if legacy:
            counts |= await seed.from_legacy_verdicts(conn)
        return counts | {"settled": (await word.sync(conn))["papers"]}

    console.print_json(data=_run(work))


@eval_app.command("replay")
def eval_replay(
    note: Annotated[str | None, typer.Option(help="What this run is trying.")] = None,
    triage_only: Annotated[bool, typer.Option(help="Skip grading.")] = False,
    config: ConfigPath = None,
) -> None:
    """Run every labelled and regression paper through the configured pipeline, as a dry run."""
    cfg = configuration.load(config)

    async def work(conn: psycopg.AsyncConnection) -> dict[str, Any]:
        papers, labelled = await feedback.replay_set(conn)
        grade = set() if triage_only else labelled
        eval_run_id = await feedback.replay(conn, cfg, papers, grade=grade, note=note)
        return await feedback.metrics(conn, eval_run_id)

    console.print_json(data=_run(work))


@eval_app.command("sample-rejects")
def eval_sample_rejects(
    seed_: Annotated[int, typer.Option("--seed", help="Recorded with the sample.")],
    pool: Annotated[int, typer.Option(help="Stored papers to triage, drawn at random.")] = 3000,
    near: Annotated[int, typer.Option(help="Rejects to draw from Radar fields.")] = 70,
    far: Annotated[int, typer.Option(help="Rejects to draw from everything else.")] = 30,
    config: ConfigPath = None,
) -> None:
    """Draw triage rejects for the owner to skim: the only look at what the gate drops."""
    cfg = configuration.load(config)

    async def work(conn: psycopg.AsyncConnection) -> dict[str, Any]:
        eval_run_id = await feedback.sample_rejects(
            conn, cfg, seed=seed_, pool=pool, near=near, far=far
        )
        return await feedback.metrics(conn, eval_run_id)

    console.print_json(data=_run(work))


@eval_app.command("report")
def eval_report(eval_run_id: int) -> None:
    """The metrics of an evaluation run, against the labels as they are now."""
    console.print_json(data=_run(lambda conn: feedback.metrics(conn, eval_run_id)))


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
    titles and abstracts from the configured database and writes only to `out`; a run resumes
    where it stopped."""
    import tomllib  # noqa: PLC0415

    from litwatch.config import Models, RoleConfig  # noqa: PLC0415
    from litwatch.llm.gateway import build  # noqa: PLC0415
    from litwatch.quality import classify  # noqa: PLC0415

    roles = tomllib.loads(candidates.read_text())
    tree, evaluation = seeds.taxonomy(), classify.load(labels)
    ids = evaluation.papers[:limit]
    found = _run(lambda conn: papers_store.get_many(conn, ids), migrate=False)
    if missing := len(ids) - len(found):
        console.print(f"[yellow]{missing} papers are not in the database and are left out[/yellow]")
    for name in candidate:
        gateway = build(Models(classify=RoleConfig.model_validate(roles[name])))

        def progress(r: classify.Result, name: str = name, done: list[int] = [0]) -> None:  # noqa: B006
            done[0] += 1
            if r.error or done[0] % 100 == 0:
                console.print(f"{name}: {done[0]} {r.paper_id} {r.error or ''}")

        new = asyncio.run(
            classify.run(
                gateway, tree, found, out / f"{name}.jsonl",
                concurrency=concurrency, progress=progress,
            )
        )  # fmt: skip
        cost = sum(r.cost or 0 for r in new)
        failed = sum(r.error is not None for r in new)
        console.print(f"{name}: {len(new)} sorted, {failed} failed, ${cost:.2f}")


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


@eval_app.command("writers")
def eval_writers(
    model: Annotated[
        list[str], typer.Option(help="provider:model@effort to compare with the configured writer.")
    ],
    out: Annotated[Path, typer.Option(help="Directory for takes.md and key.json.")],
    papers: Annotated[int, typer.Option(help="Pairs to write for.")] = 20,
    seed_: Annotated[int, typer.Option("--seed", help="Shuffles the letters.")] = 1,
    config: ConfigPath = None,
) -> None:
    """Takes from several models for the same papers, under shuffled letters, to read blind."""
    from litwatch.review import compare  # noqa: PLC0415

    cfg = configuration.load(config)

    async def work(conn: psycopg.AsyncConnection) -> Path:
        async with model_run(conn, cfg, "eval-writers") as run:
            return await compare.compare(conn, cfg, run, model, papers, out, seed_)

    console.print(f"wrote {_run(work)}/takes.md; the key is key.json beside it")


# -- library ---------------------------------------------------------------------------------------


@library_app.command("sync")
def library_sync() -> None:
    """Settle every paper with a verdict or the owner's word: effective grades, the library,
    and their events. Run once on an adopted or seeded database."""
    console.print_json(data=_run(word.sync))


# -- the writer and issues ------------------------------------------------------------------------


@app.command()
def write(config: ConfigPath = None) -> None:
    """Write a take for every pair owed one, as the nightly run does after grading."""
    cfg = configuration.load(config)

    async def work(conn: psycopg.AsyncConnection) -> dict[str, Any]:
        async with run_lock(conn), model_run(conn, cfg, "writer") as run:
            await digest_job.write_takes(conn, cfg, run)
            return run.counts

    console.print_json(data=_run(work))


@app.command()
def weekly(
    week: Annotated[
        str | None, typer.Option(help="The week's Monday, YYYY-MM-DD; default the last full week.")
    ] = None,
    config: ConfigPath = None,
) -> None:
    """Review the week for every topic and build the weekly issue, as Monday's run does."""
    cfg = configuration.load(config)
    monday = cfg.schedule.week_of(date.fromisoformat(week)) if week else None

    async def work(conn: psycopg.AsyncConnection) -> dict[str, Any]:
        async with run_lock(conn), model_run(conn, cfg, "weekly") as run:
            issue_id = await review_jobs.weekly(conn, cfg, run, monday)
            return {"issue": issue_id, **run.counts}

    console.print_json(data=_run(work))


@app.command()
def publications(
    paper: Annotated[
        str | None, typer.Option(help="Look up this one paper, whether saved or not.")
    ] = None,
    limit: Annotated[int, typer.Option(help="At most this many library papers.")] = 200,
) -> None:
    """Look up published versions on DBLP for the library papers that are due, as the
    scheduler does daily, or for one paper."""

    async def work(conn: psycopg.AsyncConnection) -> Any:
        if paper:
            found = await lookups.look_up(conn, Dblp(), paper)
            return found.model_dump(mode="json") if found else None
        return await lookups.run(conn, Dblp(), limit)

    console.print_json(data=_run(work))


@issue_app.command("build")
def issue_build(
    day: Annotated[str | None, typer.Option(help="YYYY-MM-DD; default today.")] = None,
    config: ConfigPath = None,
) -> None:
    """Take every pair ready for an issue into the day's issue and write its lede."""
    cfg = configuration.load(config)
    when = date.fromisoformat(day) if day else None

    async def work(conn: psycopg.AsyncConnection) -> int | None:
        async with run_lock(conn), model_run(conn, cfg, "issue") as run:
            return await digest_job.build_daily(conn, cfg, run, when)

    issue_id = _run(work)
    console.print(f"issue {issue_id}" if issue_id else "nothing for an issue")


@issue_app.command("show")
def issue_show(
    day: Annotated[str | None, typer.Argument(help="YYYY-MM-DD; default the latest.")] = None,
) -> None:
    """Print a daily issue as the reader would read it."""

    async def work(conn: psycopg.AsyncConnection) -> tuple[Any, list[Any]] | None:
        found = (
            await digest_store.issue(conn, "daily", date.fromisoformat(day))
            if day
            else next(iter(await digest_store.issues(conn, 1)), None)
        )
        return (found, await digest_store.entries(conn, found["id"])) if found else None

    found = _run(work)
    if found is None:
        raise SystemExit("no such issue")
    issue, entries = found
    console.print(f"[bold]LITWATCH[/bold] · {issue['day']:%A %-d %B %Y}\n")
    console.print((issue["lede"] or "[dim](no lede yet)[/dim]") + "\n")
    section = None
    for e in entries:
        if e["topic_name"] != section:
            section = e["topic_name"]
            console.print(f"[bold]{section}[/bold]")
        mark = "■" if e["grade"] == "must_read" else "·"
        aside = (
            "  [dim](set aside)[/dim]" if e["grade_now"] not in ("must_read", "relevant") else ""
        )
        console.print(f"  {mark} {e['title']}{aside}")
        console.print(f"    {e['take'] or '[dim](no take)[/dim]'}", highlight=False)
    for topic, n in issue["folds"].items():
        console.print(f"[dim]{n} more near {topic}[/dim]")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8080,
    config: ConfigPath = None,
) -> None:
    """Serve the API, the web app and the labelling page, with the integrations the
    configuration enables. Needs LITWATCH_API_TOKEN; LITWATCH_READ_TOKEN, if set, is a
    second token that may only search and read papers (for the deep service)."""
    import uvicorn  # noqa: PLC0415

    from litwatch.api import create_app  # noqa: PLC0415

    configuration.load_dotenv()
    token = configuration.secret("LITWATCH_API_TOKEN")
    if not token:
        raise SystemExit("LITWATCH_API_TOKEN (or LITWATCH_API_TOKEN_FILE) is not set")
    from litwatch import integrations  # noqa: PLC0415

    cfg = configuration.load(config)
    try:
        loaded = integrations.load(cfg)
    except integrations.IntegrationError as exc:
        raise SystemExit(str(exc)) from exc
    _run(store.migrate, migrate=False)
    app_ = create_app(
        configuration.database_url(),
        token,
        cfg,
        integrations=loaded,
        read_token=configuration.secret("LITWATCH_READ_TOKEN"),
    )
    uvicorn.run(app_, host=host, port=port)


@app.command("openapi")
def openapi(out: Annotated[Path, typer.Argument(help="Where to write the schema.")]) -> None:
    """Write the API's OpenAPI schema, which the web app's client is generated from."""
    import json  # noqa: PLC0415

    from litwatch.api import create_app  # noqa: PLC0415

    schema = create_app("postgresql://unused", "unused").openapi()
    out.write_text(json.dumps(schema, indent=2) + "\n")
    console.print(f"wrote {out}")


@app.command()
def ping(
    role: Annotated[str, typer.Argument(help="triage, judge, check or review")],
    config: ConfigPath = None,
) -> None:
    """Send one small request through a role, to check its key, model and price."""
    from pydantic import BaseModel  # noqa: PLC0415

    from litwatch import llm  # noqa: PLC0415

    class Sum(BaseModel):
        total: int

    configuration.load_dotenv()
    cfg = configuration.load(config)
    request = llm.Request(
        system="You answer arithmetic questions with the structured output only.",
        user="What is 17 + 25?",
        output=Sum,
    )
    answer = asyncio.run(llm.build(cfg.models).ask(role, request))
    usage = answer.usage
    console.print(
        f"{answer.model} -> {answer.value.total}  "
        f"({usage.prompt_tokens} in, {usage.completion_tokens} out, cost {usage.cost}, "
        f"served by {usage.provider})"
    )


if __name__ == "__main__":
    app()
