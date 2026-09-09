"""Command line entry points: screening, eval, runs, routing, selections, deep processing."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from litwatch import arxiv_source, db, evalset, evaluation, ingest
from litwatch.api import (
    SelectionsSync,
    active_selections,
    build_cascade,
    build_deep,
    build_digester,
    build_router,
    sync_selections,
)
from litwatch.config import Settings
from litwatch.digest_history import write_run_digests
from litwatch.models import TIERS, StageRole
from litwatch.selection import Selection, load_specs, seeds

app = typer.Typer(no_args_is_help=True, add_completion=False)
selections_app = typer.Typer(no_args_is_help=True, help="Manage selections in the database.")
app.add_typer(selections_app, name="selections")
console = Console()

LABELED = Path("evals/labeled.yaml")
RUNS = Path("evals/runs")
SpecOption = Annotated[
    Path | None,
    typer.Option(
        "--spec",
        help="Selection YAML file or directory to screen against instead of the database",
    ),
]


def _log() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _selections(settings: Settings, spec: Path | None) -> list[Selection]:
    """`--spec` wins; then the database's active selections; then the packaged seeds."""
    if spec is not None:
        return load_specs(spec)
    if settings.database_url:

        async def go():
            async with await db.connect(settings.database_url) as conn:
                await db.apply_schema(conn)
                return await active_selections(conn)

        found = asyncio.run(go())
        if found:
            return found
    console.print("[dim]no selections in the database; using the packaged seeds[/dim]")
    return seeds()


def _print_verdicts(result) -> None:
    for slug, v in sorted(result.verdicts.items(), key=lambda kv: (kv[1].tier, kv[0])):
        console.print(f"  tier {v.tier}  {slug:22} {v.confidence.value:6} {v.reason}")


@app.command()
def screen(
    paper_id: Annotated[str, typer.Argument(help="arXiv id or URL")],
    spec: SpecOption = None,
    rescreen: Annotated[bool, typer.Option(help="Apply the second-stage re-screen rule")] = True,
    confirm: Annotated[bool, typer.Option(help="Confirm tier 1 and 2 with the third stage")] = True,
) -> None:
    """Screen one arXiv paper against the selections and print the verdicts."""
    settings = Settings.from_env()
    papers = arxiv_source.fetch_by_ids([arxiv_source.id_from_url(paper_id)])
    if not papers:
        raise typer.BadParameter(f"arXiv returned nothing for {paper_id}")
    cascade = build_cascade(settings, _selections(settings, spec), rescreen, confirm)
    result = asyncio.run(cascade.screen(papers[0]))
    console.print(f"[bold]{papers[0].title}[/bold]  best tier {result.best_tier}")
    _print_verdicts(result)
    console.print(
        f"stages: {', '.join(f'{s.role.value} ({s.model})' for s in result.stages)}; "
        f"cost ${result.cost:.4f}"
    )


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


@app.command("eval")
def eval_cmd(
    labeled: Annotated[Path, typer.Option(help="Labeled set YAML")] = LABELED,
    spec: SpecOption = None,
    rescreen: Annotated[bool, typer.Option(help="Apply the second-stage re-screen rule")] = True,
    confirm: Annotated[bool, typer.Option(help="Confirm tier 1 and 2 with the third stage")] = True,
    concurrency: int = 8,
) -> None:
    """Run the cascade over the labeled set; report precision and recall per selection and tier."""
    settings = Settings.from_env()
    selections = _selections(settings, spec)
    items = evalset.load(labeled).papers
    cascade = build_cascade(settings, selections, rescreen, confirm)
    report = asyncio.run(evaluation.run(cascade, items, concurrency=concurrency))

    console.print(
        f"{len(report.scored)} labeled papers, {report.rescreened} re-screened, "
        f"{report.confirmed} confirmed; selections {', '.join(s.slug for s in selections)}"
    )
    for slug, sel in report.selections.items():
        if not sel.scored:
            console.print(f"\n[dim]{slug}: no labels[/dim]")
            continue
        table = Table(title=f"{slug}: {len(sel.scored)} labeled")
        table.add_column("tier")
        for role in StageRole:
            table.add_column(f"P {role.value}")
            table.add_column(f"R {role.value}")
        table.add_column("n")
        for tier in TIERS:
            cells = [str(tier)]
            for role in StageRole:
                prf = sel.per_stage[role][tier]
                cells += [_fmt(prf.precision), _fmt(prf.recall)]
            final = sel.per_tier[tier]
            table.add_row(*cells, str(final.tp + final.fn))
        console.print(table)
        console.print(f"tier 1+2 recall: {_fmt(sel.recommended_recall)}")
        if sel.misses:
            console.print(f"[red]{len(sel.misses)} tier 1 papers missed:[/red]")
            for s in sel.misses:
                got = s.predicted(slug)
                reason = s.result.verdicts[slug].reason if s.result else s.error
                console.print(
                    f"  {s.item.paper.id}  {s.item.paper.title}\n    -> tier {got}: {reason}"
                )
    costs = report.cost_by_model
    console.print(
        f"\ncost: ${sum(costs.values()):.4f} ("
        + ", ".join(f"{m} ${c:.4f}" for m, c in costs.items())
        + ")"
    )
    if report.errors:
        console.print(f"\n[red]{len(report.errors)} errors[/red]")
        for s in report.errors:
            console.print(f"  {s.item.paper.id}  {s.error}")

    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(
        json.dumps(
            {
                "labeled": str(labeled),
                "selections": {s.slug: s.version for s in selections},
                "screen_model": settings.screen_model,
                "rescreen_model": settings.rescreen_model if rescreen else None,
                "confirm_model": settings.confirm_model if confirm else None,
                "cost_by_model": costs,
                "results": [
                    {
                        "id": s.item.paper.id,
                        "expected": s.item.expected,
                        "predicted": {k: v.tier for k, v in s.result.verdicts.items()}
                        if s.result
                        else None,
                        "result": s.result.model_dump() if s.result else None,
                        "error": s.error,
                    }
                    for s in report.scored
                ],
            },
            indent=2,
        )
    )
    console.print(f"\nrun written to {out}")


@app.command()
def candidates(
    query: Annotated[str, typer.Argument(help="arXiv query, e.g. 'abs:faithful AND abs:CoT'")],
    out: Annotated[Path, typer.Option(help="YAML file to append to, created if missing")],
    max_results: Annotated[int, typer.Option("--max")] = 30,
    spec: SpecOption = None,
    prelabel: Annotated[
        bool, typer.Option(help="Fill expected tiers from the screener as a draft")
    ] = False,
) -> None:
    """Search arXiv and append new papers to a YAML file in the labeled-set format."""
    existing = evalset.load(out) if out.exists() else evalset.EvalSet()
    known = {p.paper.id for p in existing.papers}
    found = [p for p in arxiv_source.search(query, max_results) if p.id not in known]
    console.print(f"{len(found)} new papers for {query!r}")

    cascade = None
    if prelabel:
        settings = Settings.from_env()
        cascade = build_cascade(settings, _selections(settings, spec))
    spent = 0.0

    async def label(paper):
        nonlocal spent
        result = await cascade.screen(paper)
        spent += result.cost
        best = result.best
        note = f"draft ({result.model}): " + (
            f"{best[0]} tier {best[1].tier}, {best[1].confidence}: {best[1].reason}"
            if best
            else "member of no selection"
        )
        return evalset.LabeledPaper(
            paper=paper, expected={k: v.tier for k, v in result.verdicts.items()}, note=note
        )

    async def label_all():
        return await asyncio.gather(*(label(p) for p in found))

    if cascade:
        new_items = asyncio.run(label_all())
        console.print(f"cost: ${spent:.4f}")
    else:
        new_items = [evalset.LabeledPaper(paper=p) for p in found]
    existing.papers.extend(new_items)
    evalset.save(out, existing)
    console.print(f"{len(existing.papers)} papers in {out}")


@app.command()
def run(
    days: Annotated[int, typer.Option(help="Fetch window in days ending now")] = 3,
    kind: Annotated[str, typer.Option(help="daily, backfill, or manual")] = "manual",
    fetch: Annotated[bool, typer.Option(help="Fetch before screening pending papers")] = True,
) -> None:
    """Fetch the arXiv window into the database and screen everything pending."""
    _log()
    settings = Settings.from_env()

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            await db.apply_schema(conn)
            selections = await active_selections(conn)
            if not selections:
                raise SystemExit("no active selections; run `litwatch selections seed` first")
            return await ingest.run(
                conn,
                settings,
                build_cascade(settings, selections),
                kind,
                days,
                fetch,
                router=build_router(settings),
                digester=build_digester(settings),
                deep=build_deep(settings, selections),
            )

    s = asyncio.run(go())
    console.print(
        f"run {s.run_id}: fetched {s.fetched} ({s.inserted} new), screened {s.screened}, "
        f"{s.rescreened} re-screened, {s.failed} failed, cost ${s.cost:.4f}, tiers {s.tiers}, "
        f"routed {s.routed} ({s.route_failed} failed), "
        f"deep-processed {s.processed} ({s.process_failed} failed, list ${s.deep_list_cost:.2f})"
    )


@app.command()
def route(
    paper: Annotated[
        str | None, typer.Option(help="One arXiv id; fetched and screened first if unknown")
    ] = None,
    pdf: Annotated[bool, typer.Option(help="Attach the arXiv PDF for tier 1")] = True,
) -> None:
    """Route tier 1/2 papers to Zotero (item, collection, tags, screen note, PDF) and release
    papers that dropped below tier 2."""
    _log()
    settings = Settings.from_env()
    router = build_router(settings, attach_pdf=pdf)
    if router is None:
        raise SystemExit("ZOTERO_API_KEY and LITWATCH_ZOTERO_USER_ID are not set")

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            await db.apply_schema(conn)
            if paper and not await db.get_paper(conn, paper):
                found = arxiv_source.fetch_by_ids([paper])
                if not found:
                    raise SystemExit(f"{paper} not found on arXiv")
                await db.upsert_papers(conn, found)
                cascade = build_cascade(settings, await active_selections(conn))
                result = await cascade.screen(found[0])
                await db.store_screen(conn, result)
                console.print(f"{paper}: screened as tier {result.best_tier}")
            failures = await ingest.route_pending(conn, router, paper_id=paper)
            rows = await (
                await conn.execute(
                    "SELECT id, routed_tier, zotero_key, zotero_attachment_key FROM papers "
                    "WHERE routed_at >= now() - interval '1 minute' ORDER BY routed_at"
                )
            ).fetchall()
            return rows, failures

    rows, failures = asyncio.run(go())
    for r in rows:
        console.print(
            f"  {r['id']} tier {r['routed_tier']} item={r['zotero_key']} "
            f"pdf={r['zotero_attachment_key']}"
        )
    for pid, err in failures:
        console.print(f"  [red]{pid} failed: {err}[/red]")
    console.print(f"routed {len(rows)}, failed {len(failures)}")


@app.command()
def reconfirm() -> None:
    """Run the confirm stage on tier 1/2 verdicts that were never confirmed."""
    _log()
    settings = Settings.from_env()

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            cascade = build_cascade(settings, await active_selections(conn))
            todo = await db.unconfirmed_positives(conn)
            sem = asyncio.Semaphore(settings.screen_concurrency)
            lock = asyncio.Lock()
            changed, cost = [], 0.0

            async def one(paper, prior):
                nonlocal cost
                async with sem:
                    result = await cascade.confirm_existing(paper, prior)
                async with lock:
                    await db.store_screen(conn, result)
                    cost += sum(s.usage.cost or 0.0 for s in result.stages[len(prior.stages) :])
                    for slug, v in result.verdicts.items():
                        if v.tier != prior.tier_for(slug):
                            changed.append((paper, slug, prior.tier_for(slug), v.tier))

            await asyncio.gather(*(one(p, r) for p, r in todo))
            return len(todo), changed, cost

    n, changed, cost = asyncio.run(go())
    console.print(f"confirmed {n} papers, {len(changed)} verdicts changed, cost ${cost:.4f}")
    for paper, slug, before, after in changed:
        console.print(f"  {paper.id} {slug} {before} -> {after}  {paper.title[:70]}")


@app.command()
def rescreen(
    selection: Annotated[str, typer.Option(help="Selection slug to judge again")],
    days: Annotated[
        int | None, typer.Option(help="Only papers published in the last N days (default: all)")
    ] = None,
) -> None:
    """Forget a selection's verdicts so the next run judges those papers again."""
    settings = Settings.from_env()

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            return await db.forget_verdicts(conn, selection, days)

    console.print(f"forgot {asyncio.run(go())} verdicts for {selection}; run `litwatch run` next")


@selections_app.command("list")
def selections_list() -> None:
    """Selections in the database with their versions and member counts."""
    settings = Settings.from_env()

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            await db.apply_schema(conn)
            return await db.list_selections(conn), await db.selection_counts(conn)

    rows, counts = asyncio.run(go())
    table = Table()
    for col in ("slug", "status", "version", "rubric", "tier 1", "tier 2", "tier 3", "owner"):
        table.add_column(col)
    for s in rows:
        c = counts.get(s.slug, {})
        table.add_row(
            s.slug,
            s.status.value,
            s.version,
            "yes" if s.has_rubric else "no",
            str(c.get(1, 0)),
            str(c.get(2, 0)),
            str(c.get(3, 0)),
            s.owner or "",
        )
    console.print(table)


@selections_app.command("show")
def selections_show(slug: str) -> None:
    settings = Settings.from_env()

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            return [s for s in await db.list_selections(conn) if s.slug == slug]

    found = asyncio.run(go())
    if not found:
        raise SystemExit(f"no selection {slug}")
    console.print_json(found[0].model_dump_json())


@selections_app.command("put")
def selections_put(
    path: Annotated[Path, typer.Argument(help="Selection YAML file or directory")],
) -> None:
    """Upsert selections from YAML without pausing the others (seeds, experiments)."""
    settings = Settings.from_env()
    rows = [s.model_dump() for s in load_specs(path)]

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            await db.apply_schema(conn)
            return await sync_selections(conn, SelectionsSync(selections=rows, authoritative=False))

    for r in asyncio.run(go()):
        console.print(r)


@selections_app.command("seed")
def selections_seed() -> None:
    """Upsert the selections shipped with the package."""
    settings = Settings.from_env()
    rows = [s.model_dump() for s in seeds()]

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            await db.apply_schema(conn)
            return await sync_selections(conn, SelectionsSync(selections=rows, authoritative=False))

    for r in asyncio.run(go()):
        console.print(r)


@app.command()
def digest(
    run_id: Annotated[int | None, typer.Option("--run", help="Run id from the runs table")] = None,
    days: Annotated[
        int | None, typer.Option(help="Instead of a run: papers screened in the last N days")
    ] = None,
    model: Annotated[str | None, typer.Option(help="Override the digest model")] = None,
    store: Annotated[bool, typer.Option(help="Store the digests for the run")] = False,
) -> None:
    """Write the digests for one run (or a recent window) and print them."""
    if (run_id is None) == (days is None):
        raise SystemExit("give exactly one of --run or --days")
    if store and days is not None:
        raise SystemExit("--store requires --run; --days is a retrospective preview")
    settings = Settings.from_env()
    digester = build_digester(settings, model)

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            selections = await active_selections(conn)
            if run_id is not None:
                digests = await write_run_digests(conn, digester, selections, run_id, store=store)
            else:
                assert days is not None
                members = await db.members_since(conn, datetime.now(UTC) - timedelta(days=days))
                digests = await digester.write_all(selections, members)
            return digests

    digests = asyncio.run(go())
    for d in digests:
        print(f"===== {d.title} =====\n{d.body}\n")  # raw: the console printer would wrap
    cost = sum(d.cost or 0.0 for d in digests)
    console.print(f"[dim]{digester.model}, cost ${cost:.4f}[/dim]")


@app.command()
def deep(
    paper: Annotated[
        str | None, typer.Option(help="One arXiv id, processed even if it was tried before")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Pending papers to process at most")] = None,
) -> None:
    """Deep-process pending tier 1 papers (or one paper) and write the results to Zotero."""
    _log()
    settings = Settings.from_env()

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            await db.apply_schema(conn)
            processor = build_deep(settings, await active_selections(conn))
            if processor is None:
                raise SystemExit("LITWATCH_DEEP_URL is not set")
            return await processor.process_pending(conn, paper, limit or settings.deep_max_per_run)

    s = asyncio.run(go())
    for pid, err in s.failures:
        console.print(f"  [red]{pid}: {err}[/red]")
    console.print(f"processed {s.processed}, failed {s.failed}, list cost ${s.list_cost:.2f}")


@app.command("deep-status")
def deep_status() -> None:
    """What litwatch-deep is doing: auth state, running job, recent jobs, pending papers."""
    settings = Settings.from_env()
    if not settings.deep_url:
        raise SystemExit("LITWATCH_DEEP_URL is not set")
    from litwatch.deep import DeepClient

    async def go():
        client = DeepClient(settings.deep_url)
        health, jobs = await client.health(), await client.jobs()
        async with await db.connect(settings.require_database()) as conn:
            pending = await db.deep_pending(conn)
        return health, jobs, pending

    health, jobs, pending = asyncio.run(go())
    console.print(
        f"deep: {health.get('model')} auth={health.get('auth')} "
        f"running={health.get('running')} queued={health.get('queued')}"
    )
    for j in jobs[:10]:
        st = j.get("stats") or {}
        console.print(
            f"  {j['paper_id']} {j['status']:7} cells={st.get('cells', 0):2} "
            f"turns={st.get('turns', 0):2} ${st.get('list_cost', 0):.2f} {st.get('seconds', 0)}s "
            f"{j.get('last_event', '')} {j.get('error') or ''}"
        )
    console.print(
        f"pending in litwatch: {len(pending)}"
        + (": " + ", ".join(p["id"] for p in pending) if pending else "")
    )


@app.command()
def trace(
    paper: Annotated[str, typer.Argument(help="arXiv id")],
    ipynb: Annotated[str | None, typer.Option(help="Write the trace as a notebook here")] = None,
) -> None:
    """Show a paper's deep-processing trace, or export it as a Jupyter notebook."""
    from litwatch.notebook import trace_notebook

    settings = Settings.from_env()

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            return await db.get_paper(conn, paper)

    row = asyncio.run(go())
    if row is None or not row.get("deep_trace"):
        raise SystemExit(f"{paper}: no trace stored")
    if ipynb:
        with open(ipynb, "w") as f:
            json.dump(trace_notebook(row), f, indent=1)
        console.print(f"wrote {ipynb}")
        return
    for ev in row["deep_trace"]:
        if ev["type"] == "cell":
            console.rule(f"cell {ev['n']} ({ev['ms']} ms)")
            print(ev["code"])
            if ev.get("output"):
                console.print(ev["output"][:2000], style="dim", markup=False, highlight=False)
            if ev.get("error"):
                console.print(f"[red]{ev['error']}[/red]")
        elif ev["type"] == "assistant" and (ev.get("text") or ev.get("thinking")):
            console.rule("assistant")
            print(ev.get("thinking") or "", ev.get("text") or "")
        elif ev["type"] == "submit":
            console.rule("submit " + ("ok" if ev["ok"] else "rejected"))
            for e in ev.get("errors") or []:
                print("  " + e)
    st = row.get("deep_stats") or {}
    console.print(f"{st}")


@app.command()
def stats() -> None:
    """Row counts per best tier and per selection."""
    settings = Settings.from_env()

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            return await db.stats(conn)

    console.print_json(json.dumps(asyncio.run(go()), default=str))


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8080) -> None:  # noqa: S104 - stack-internal
    """Serve the internal HTTP API used by n8n."""
    import uvicorn

    from litwatch.api import create_app

    _log()
    uvicorn.run(create_app(Settings.from_env()), host=host, port=port, log_level="info")
