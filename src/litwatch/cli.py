"""The jobs and a few queries from a terminal."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import psycopg
import typer
import yaml
from rich.console import Console

from litwatch import config as configuration
from litwatch import seeds, store
from litwatch.archive import ArchiveError
from litwatch.core import Topic
from litwatch.feedback import word
from litwatch.integrations import IntegrationError
from litwatch.integrations import archive as archive_integration
from litwatch.jobs import intake as intake_job
from litwatch.jobs import nightly as nightly_job
from litwatch.jobs import publications as lookups
from litwatch.jobs.lock import run_lock
from litwatch.publications import Dblp
from litwatch.store import papers as papers_store
from litwatch.store import runs
from litwatch.store import topics as topics_store

app = typer.Typer(no_args_is_help=True, add_completion=False, help="litwatch")
db_app = typer.Typer(no_args_is_help=True, help="Database migrations.")
topics_app = typer.Typer(no_args_is_help=True, help="Topics as YAML, in and out.")
eval_app = typer.Typer(no_args_is_help=True, help="Dry runs measured against the owner's labels.")
library_app = typer.Typer(no_args_is_help=True, help="Effective grades and the library.")
taxonomy_app = typer.Typer(no_args_is_help=True, help="The class tree and its followed topics.")
intake_app = typer.Typer(help="Take papers in: from arXiv, or a window from the archive.")
app.add_typer(db_app, name="db")
app.add_typer(topics_app, name="topics")
app.add_typer(eval_app, name="eval")
app.add_typer(library_app, name="library")
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
    config: ConfigPath = None,
) -> None:
    """The nightly run as the scheduler runs it: intake, for now."""
    cfg = configuration.load(config)
    run_id = _run(lambda conn: nightly_job.run(conn, cfg, fetch=fetch, days=days))
    run = _run(lambda conn: runs.get(conn, run_id))
    console.print(f"run {run_id}: cost ${run['cost']}")
    console.print_json(data=run["counts"])


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
    settled again (a topic no longer Watch drops its grades)."""
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
) -> None:
    """Create or update topics. A changed topic gets a new revision and applies forward."""
    if (path is not None) == starter:
        raise typer.BadParameter("give a path or --starter")

    async def work(conn: psycopg.AsyncConnection) -> list[Topic]:
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


# -- library ---------------------------------------------------------------------------------------


@library_app.command("sync")
def library_sync() -> None:
    """Settle every paper with a verdict or the owner's word: effective grades, the library,
    and their events. For a database written around the settle."""
    console.print_json(data=_run(word.sync))


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


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8080,
    config: ConfigPath = None,
) -> None:
    """Serve the API and the web app, with the integrations the
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
    role: Annotated[str, typer.Argument(help="classify, judge, check or writer")],
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
