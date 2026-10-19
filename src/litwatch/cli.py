"""The jobs and a few queries from a terminal."""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import psycopg
import typer
import yaml
from rich.console import Console

from litwatch import config as configuration
from litwatch import logs, seeds, store
from litwatch.archive import ArchiveError
from litwatch.core import Paper, Taxonomy
from litwatch.feedback import word
from litwatch.integrations import IntegrationError
from litwatch.integrations import archive as archive_integration
from litwatch.jobs import intake as intake_job
from litwatch.jobs import nightly as nightly_job
from litwatch.jobs import publications as lookups
from litwatch.jobs.lock import BusyError, run_lock
from litwatch.publications import Dblp
from litwatch.store import papers as papers_store
from litwatch.store import runs
from litwatch.store import taxonomy as taxonomy_store

app = typer.Typer(no_args_is_help=True, add_completion=False, help="litwatch")
db_app = typer.Typer(no_args_is_help=True, help="Database migrations.")
eval_app = typer.Typer(no_args_is_help=True, help="Dry runs measured against the owner's labels.")
library_app = typer.Typer(no_args_is_help=True, help="Effective grades, rarity and the library.")
taxonomy_app = typer.Typer(no_args_is_help=True, help="The class tree and its topics, as YAML.")
intake_app = typer.Typer(help="Take papers in: from arXiv, or a window from the archive.")
digests_app = typer.Typer(no_args_is_help=True, help="Daily and weekly digests.")
app.add_typer(db_app, name="db")
app.add_typer(eval_app, name="eval")
app.add_typer(library_app, name="library")
app.add_typer(taxonomy_app, name="taxonomy")
app.add_typer(intake_app, name="intake")
app.add_typer(digests_app, name="digests")
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


def _run[T](work: Callable[[psycopg.AsyncConnection], Awaitable[T]], *, migrate: bool = True) -> T:
    """Run `work` on a connection to the configured database, migrated first."""
    logs.setup()

    async def main() -> T:
        async with await store.connect(configuration.database_url()) as conn:
            if migrate:
                await store.migrate(conn)
            return await work(conn)

    try:
        return asyncio.run(main())
    except (store.LegacyDatabaseError, BusyError) as exc:
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
    config: ConfigPath = None,
) -> None:
    """The nightly run as the scheduler runs it: intake, classification, analysis, grading,
    the full-text passes and the digests."""
    cfg = configuration.load(config)
    run_id = _run(
        lambda conn: nightly_job.run(
            conn, cfg, fetch=fetch, days=days, limit=limit, with_digests=digests
        )
    )
    run = _run(lambda conn: runs.get(conn, run_id))
    console.print(f"run {run_id}: cost ${run['cost']}")
    console.print_json(data=run["counts"])


# -- digests --------------------------------------------------------------------------------------


@digests_app.command("write")
def digests_write(since: Since, until: Until, config: ConfigPath = None) -> None:
    """Write the digests of every day from --since to --until, and the weekly digests of the
    weeks inside it, as the nightly run would have on those days, over the papers due now
    (the restart, a missed night). Published digests stay as they are."""
    from litwatch.jobs import digests as digests_job  # noqa: PLC0415

    cfg = configuration.load(config)
    run_id = _run(lambda conn: digests_job.window(conn, cfg, since.date(), until.date()))
    run = _run(lambda conn: runs.get(conn, run_id))
    console.print(f"run {run_id}: cost ${run['cost']}")
    console.print_json(data=run["counts"])


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
) -> None:
    """Make the file the taxonomy. A changed class or topic gets a new revision and applies
    forward; a class left out is retired, a topic left out unfollowed."""
    tree = _taxonomy_file(path)
    saved = _run(lambda conn: word.save_taxonomy(conn, tree))
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
    tree = _run(taxonomy_store.load)
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
    labels: Annotated[Path, typer.Option(help="The evaluation labels.")] = Path("evals/labels"),
    limit: Annotated[int | None, typer.Option(help="Only the first N papers.")] = None,
    resume: Annotated[int | None, typer.Option(help="Go on with this replay.")] = None,
    note: Annotated[str | None, typer.Option(help="A note kept with the run.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Spend without asking.")] = False,
    config: ConfigPath = None,
) -> None:
    """Run the labelled and missed papers through the current classification, keyword check
    and grading (feedback.md, evaluation). A dry run: it writes to the evaluation tables only.
    Shows what it would cost and asks first; a run cut short goes on with --resume."""
    from litwatch.quality import replay  # noqa: PLC0415

    cfg = configuration.load(config)
    sample = _counts(negatives or [], "--negatives")
    if unknown := (set(cohort or []) | set(sample)) - set(replay.COHORTS):
        raise typer.BadParameter(f"no cohort {', '.join(sorted(unknown))}")

    async def prepare(conn: psycopg.AsyncConnection) -> tuple[Any, dict[str, float]]:
        if resume is not None:
            r = await replay.resume(conn, cfg, resume)
        else:
            tree = await taxonomy_store.load(conn)
            if tree is None:
                raise ValueError("no taxonomy yet: `litwatch taxonomy import` the seed first")
            every = replay.load(labels) + await replay.misses(conn)
            chosen = replay.select(every, replay.focus(tree), cohorts=cohort, negatives=sample)
            r = await replay.prepare(conn, chosen, limit=limit)
        return r, await replay.estimate(conn, cfg, r)

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

    async def work(conn: psycopg.AsyncConnection) -> dict[str, Any]:
        if r.eval_run_id is None:
            params = {"cohorts": cohort, "negatives": sample, "seed": replay.SEED, "limit": limit}
            await replay.start(conn, cfg, r, params, note)
            console.print(f"replay {r.eval_run_id}")
        return await replay.run(conn, cfg, r)

    counts = _run(work)
    console.print_json(data=counts)
    console.print(f"report: litwatch eval report {r.eval_run_id}")


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

    async def work(conn: psycopg.AsyncConnection) -> str | None:
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

    async def work(conn: psycopg.AsyncConnection) -> str:
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
    """Write the Brief of each paper of the preference set with the configured `brief` role,
    reading its text from the archive as a full-text pass does. A dry run: it reads the papers
    from the configured database and writes only to `out`; a run resumes where it stopped."""
    from litwatch.fulltext import archive_for  # noqa: PLC0415
    from litwatch.llm.gateway import build  # noqa: PLC0415
    from litwatch.quality import briefs  # noqa: PLC0415

    cfg = configuration.load(config)
    archive = archive_for(cfg)
    if archive is None:
        raise SystemExit("no archive: configure one, or set $LITWATCH_ARCHIVE_URL")
    if text not in briefs.TEXTS:
        raise SystemExit(f"--text is one of {', '.join(briefs.TEXTS)}")
    entries = briefs.load(preferences)
    found = _run(lambda conn: papers_store.get_many(conn, [e.id for e in entries]), migrate=False)
    by_id = {p.id: p for p in found}
    if missing := [e.id for e in entries if e.id not in by_id]:
        console.print(f"[yellow]not in the database, left out: {', '.join(missing)}[/yellow]")

    def progress(r: briefs.Result) -> None:
        console.print(f"{r.paper_id}: {r.error or 'answered'} {briefs.dollars([r.cost])}")

    async def main() -> list[briefs.Result]:
        try:
            return await briefs.run(
                build(cfg.models), seeds.taxonomy(),
                [(by_id[e.id], e) for e in entries if e.id in by_id], archive, out,
                concurrency=concurrency, text=text, wait_seconds=wait_minutes * 60,
                progress=progress,
            )  # fmt: skip
        finally:
            await archive.aclose()

    new = asyncio.run(main())
    cost = briefs.dollars([r.cost for r in new])
    console.print(f"{sum(r.output is not None for r in new)} of {len(new)} answered, {cost}")


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

    async def work(conn: psycopg.AsyncConnection) -> tuple[list[Paper], dict[str, str]]:
        return await papers_store.get_many(conn, ids), await analyses_store.abstract_gists(
            conn, ids
        )

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
    `out`; a run resumes where it stopped. Shows what it would cost and asks first, and stops
    at the ceiling."""
    if not (math.isfinite(ceiling) and ceiling > 0):
        raise typer.BadParameter("--ceiling is a positive amount")
    from litwatch.fulltext import archive_for  # noqa: PLC0415
    from litwatch.llm.gateway import build  # noqa: PLC0415
    from litwatch.quality import ladders  # noqa: PLC0415

    cfg = configuration.load(config)
    found = ladders.ladders(candidates)
    if which is not None and which not in ladders.SETS:
        raise typer.BadParameter(f"--set is one of {', '.join(ladders.SETS)}")
    if unknown := set(role or []) - set(found):
        raise typer.BadParameter(f"no ladder for {', '.join(sorted(unknown))}")
    chosen = [
        c
        for c in ladders.cases(cases)
        if c.role in (role or found) and (which is None or c.set == which)
    ]
    archive = archive_for(cfg)

    async def prepare(conn: psycopg.AsyncConnection) -> dict[str, Any]:
        try:
            return await ladders.requests(conn, archive, chosen)
        finally:
            if archive is not None:
                await archive.aclose()

    built = _run(prepare, migrate=False)
    for key, why in built.items():
        if isinstance(why, str):
            console.print(f"[yellow]left out: {key}: {why}[/yellow]")
    done = ladders.rows(out)
    work = ladders.todo(chosen, found, built, done, only)
    by = Counter((t.case.role, t.candidate.name) for t in work)
    for (r, name), n in sorted(by.items()):
        cost = sum(ladders.estimate(t.ladder, t.candidate, t.request) for t in work
                   if (t.case.role, t.candidate.name) == (r, name))  # fmt: skip
        console.print(f"{r} {name}: {n} calls, estimated ${cost:.2f}")
    expected = sum(ladders.estimate(t.ladder, t.candidate, t.request) for t in work)
    console.print(
        f"{len(work)} calls, estimated ${expected:.2f}; spent so far "
        f"${ladders.spent(done, ladders.reservations(out)):.2f} of the ${ceiling:.2f} ceiling"
    )
    if not work or (not yes and not typer.confirm("Spend it?")):
        return
    providers = build(cfg.models).providers

    def progress(r: ladders.Row) -> None:
        if r.outcome != "answered":
            console.print(f"{r.case} {r.candidate}: {r.outcome} {r.detail or ''}")

    try:
        ran = asyncio.run(
            ladders.run(work, providers, out, ceiling, concurrency=concurrency, progress=progress)
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    kinds = Counter(r.outcome for r in ran.rows)
    console.print(f"{len(ran.rows)} calls: {dict(kinds)}; ${ladders.spent(ran.rows):.2f}")
    if ran.why is not None:
        t = ran.stopped_at
        first = f", first not run: {t.case.key} {t.candidate.name}" if t else ""
        console.print(f"[red]stopped: {ran.why}{first} (recorded in {out / ladders.BUDGET})[/red]")


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

    async def work(conn: psycopg.AsyncConnection) -> tuple[dict[str, Any], dict[str, str]]:
        ids = sorted({c.paper for c in chosen if c.paper and c.set == "refusals"})
        try:
            built = await ladders.requests(conn, archive, chosen)
            found_papers = await ladders.papers(conn, archive, ids)
            return built, {p.id: p.title for p in found_papers.values()}
        finally:
            if archive is not None:
                await archive.aclose()

    built, titles = _run(work, migrate=False)
    calls = ladders.rows(out)
    print(ladders.tables(found, chosen, calls, built, ladders.reservations(out)), end="")
    print("\n### Answers on the refusals set\n")
    print(ladders.answers(found, chosen, calls, built, titles), end="")


# -- library ---------------------------------------------------------------------------------------


@library_app.command("sync")
def library_sync() -> None:
    """Settle every paper with a verdict, a Weight or the owner's word: effective grades,
    rarity, the library, and their events. For a database written around the settle."""
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
    second token that may only search and read papers (for the deep service)."""
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
    _run(store.migrate, migrate=False)
    from litwatch import fulltext  # noqa: PLC0415

    app_ = create_app(
        configuration.database_url(),
        token,
        cfg,
        integrations=loaded,
        read_token=configuration.secret("LITWATCH_READ_TOKEN"),
        archive=fulltext.archive_for(cfg),
    )
    logs.setup()
    asyncio.run(_serve(app_, host, port, metrics_port))


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
    config: ConfigPath = None,
) -> None:
    """Work the full-text queue as the service's workers do: try every pass that is due once,
    and with --wait again every half minute while some still await their text from the archive
    ($LITWATCH_ARCHIVE_URL and $LITWATCH_ARCHIVE_TOKEN, or the archive integration). Text passes
    by default; Deep card passes with --deep, one at a time, each a paid job on the deep service."""
    from litwatch import fulltext  # noqa: PLC0415
    from litwatch.fulltext import deep as deep_passes  # noqa: PLC0415
    from litwatch.integrations import deep as deep_integration  # noqa: PLC0415
    from litwatch.integrations import enabled_using  # noqa: PLC0415

    cfg = configuration.load(config)
    archive = fulltext.archive_for(cfg)
    if archive is None:
        raise SystemExit("no archive configured")
    maker = None
    if deep:
        options = enabled_using(cfg, "deep")
        if options is None:
            raise SystemExit("no deep integration configured")
        options = {k: v for k, v in options.items() if k not in ("use", "enabled")}
        maker = deep_integration.load("deep", options, cfg)

    async def work_deep(conn: psycopg.AsyncConnection) -> Counter[str]:
        total: Counter[str] = Counter()
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            worked = total["cards_written"] + total["card_failed"]
            if limit is not None and worked >= limit:
                return total
            done = await deep_passes.work(
                conn, archive, maker, limit=None if limit is None else limit - worked
            )
            console.print(dict(done))
            total.update(done)
            left = deadline - asyncio.get_running_loop().time()
            if not done["awaiting_text"] or left <= 0:
                return total
            await asyncio.sleep(min(fulltext.TICK_SECONDS, left))

    async def work(conn: psycopg.AsyncConnection) -> Counter[str]:
        try:
            if maker is not None:
                return await work_deep(conn)
            return await fulltext.work_waiting(
                conn, cfg, archive, limit=limit, wait=wait, report=lambda d: console.print(dict(d))
            )
        finally:
            await archive.aclose()
            if maker is not None:
                await maker.aclose()

    console.print_json(data=dict(_run(work)))


@app.command()
def ping(
    role: Annotated[str, typer.Argument(help=", ".join(configuration.ROLES))],
    config: ConfigPath = None,
) -> None:
    """Send one small request through a role, to check its key, model and price."""
    from pydantic import BaseModel  # noqa: PLC0415

    from litwatch import llm  # noqa: PLC0415

    class Sum(BaseModel):
        total: int

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
