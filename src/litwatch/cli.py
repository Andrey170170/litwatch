"""The jobs and a few queries from a terminal."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import psycopg
import typer
from rich.console import Console

from litwatch import config as configuration
from litwatch import feedback, seeds, store
from litwatch.core import Topic
from litwatch.feedback import seed, word
from litwatch.jobs import intake as intake_job
from litwatch.jobs import nightly as nightly_job
from litwatch.jobs.lock import run_lock
from litwatch.store import legacy as legacy_store
from litwatch.store import runs
from litwatch.store import topics as topics_store
from litwatch.store import triage as triage_store

app = typer.Typer(no_args_is_help=True, add_completion=False, help="litwatch")
db_app = typer.Typer(no_args_is_help=True, help="Database migrations.")
topics_app = typer.Typer(no_args_is_help=True, help="Topics as YAML, in and out.")
eval_app = typer.Typer(no_args_is_help=True, help="Dry runs measured against the owner's labels.")
library_app = typer.Typer(no_args_is_help=True, help="Effective grades and the library.")
app.add_typer(db_app, name="db")
app.add_typer(topics_app, name="topics")
app.add_typer(eval_app, name="eval")
app.add_typer(library_app, name="library")
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


@app.command()
def intake(
    days: Annotated[int | None, typer.Option(help="Look back this many days.")] = None,
    config: ConfigPath = None,
) -> None:
    """Fetch new and revised papers for the configured categories."""
    cfg = configuration.load(config)

    async def work(conn: psycopg.AsyncConnection) -> Any:
        async with run_lock(conn):
            return await intake_job.run(conn, cfg, days)

    result = _run(work)
    console.print(
        f"intake {result.details['status']}: {result.fetched} papers, "
        f"{result.inserted} new, {result.updated} updated"
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
    """Save the topics; one whose kind changed has its papers settled again."""
    saved = []
    for topic in topics:
        # The kind is read under the row lock the save takes, so a concurrent import cannot
        # make this one miss the transition.
        async with conn.transaction():
            before = await topics_store.kind_for_update(conn, topic.slug)
            saved.append(await topics_store.save(conn, topic))
            if before is not None and before != topic.kind:
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


# -- library ---------------------------------------------------------------------------------------


@library_app.command("sync")
def library_sync() -> None:
    """Settle every paper with a verdict or the owner's word: effective grades, the library,
    and their events. Run once on an adopted or seeded database."""
    console.print_json(data=_run(word.sync))


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8080,
) -> None:
    """Serve the API and the labelling page. Needs LITWATCH_API_TOKEN."""
    import uvicorn  # noqa: PLC0415

    from litwatch.api import create_app  # noqa: PLC0415

    configuration.load_dotenv()
    token = configuration.secret("LITWATCH_API_TOKEN")
    if not token:
        raise SystemExit("LITWATCH_API_TOKEN (or LITWATCH_API_TOKEN_FILE) is not set")
    _run(store.migrate, migrate=False)
    uvicorn.run(create_app(configuration.database_url(), token), host=host, port=port)


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
