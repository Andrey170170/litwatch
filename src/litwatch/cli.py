"""Command line entry points for phase 1: screen, eval, candidates."""

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
from litwatch.api import build_cascade, build_deep, build_digester, build_router
from litwatch.config import Settings
from litwatch.models import StageRole, Tier

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

LABELED = Path("evals/labeled.yaml")
RUNS = Path("evals/runs")


@app.command()
def screen(
    paper_id: Annotated[str, typer.Argument(help="arXiv id or URL")],
    rescreen: Annotated[bool, typer.Option(help="Apply the second-stage re-screen rule")] = True,
    confirm: Annotated[bool, typer.Option(help="Confirm tier 1 and 2 with the third stage")] = True,
) -> None:
    """Screen one arXiv paper and print the result."""
    settings = Settings.from_env()
    papers = arxiv_source.fetch_by_ids([arxiv_source.id_from_url(paper_id)])
    if not papers:
        raise typer.BadParameter(f"arXiv returned nothing for {paper_id}")
    result = asyncio.run(build_cascade(settings, rescreen, confirm).screen(papers[0]))
    console.print(f"[bold]{papers[0].title}[/bold]")
    console.print_json(result.model_dump_json())
    console.print(f"cost: ${result.cost:.4f}")


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


@app.command("eval")
def eval_cmd(
    labeled: Annotated[Path, typer.Option(help="Labeled set YAML")] = LABELED,
    rescreen: Annotated[bool, typer.Option(help="Apply the second-stage re-screen rule")] = True,
    confirm: Annotated[bool, typer.Option(help="Confirm tier 1 and 2 with the third stage")] = True,
    concurrency: int = 8,
) -> None:
    """Run the cascade over the labeled set; report precision and recall after each stage."""
    settings = Settings.from_env()
    items = evalset.load(labeled).papers
    report = asyncio.run(
        evaluation.run(build_cascade(settings, rescreen, confirm), items, concurrency=concurrency)
    )

    table = Table(
        title=f"{len(report.scored)} labeled papers, {report.rescreened} re-screened, "
        f"{report.confirmed} confirmed"
    )
    table.add_column("tier")
    for role in StageRole:
        table.add_column(f"P {role.value}")
        table.add_column(f"R {role.value}")
    table.add_column("n")
    for tier in Tier:
        cells = [tier.value]
        for role in StageRole:
            prf = report.per_stage[role][tier]
            cells += [_fmt(prf.precision), _fmt(prf.recall)]
        final = report.per_tier[tier]
        table.add_row(*cells, str(final.tp + final.fn))
    console.print(table)
    agreement = evaluation.flag_agreement(report.scored)
    console.print("flag accuracy: " + ", ".join(f"{k}={_fmt(v)}" for k, v in agreement.items()))
    costs = report.cost_by_model
    console.print(
        f"cost: ${sum(costs.values()):.4f} ("
        + ", ".join(f"{m} ${c:.4f}" for m, c in costs.items())
        + ")"
    )

    if report.misses:
        console.print(f"\n[red]{len(report.misses)} must_read papers missed:[/red]")
        for s in report.misses:
            reason = s.result.output.reason if s.result else s.error
            console.print(f"  {s.item.paper.id}  {s.item.paper.title}\n    -> {reason}")
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
                "screen_model": settings.screen_model,
                "rescreen_model": settings.rescreen_model if rescreen else None,
                "confirm_model": settings.confirm_model if confirm else None,
                "cost_by_model": costs,
                "results": [
                    {
                        "id": s.item.paper.id,
                        "expected": s.expected_tier.value,
                        "predicted": s.predicted_tier.value if s.predicted_tier else None,
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
    prelabel: Annotated[
        bool, typer.Option(help="Fill expected flags from the screener as a draft")
    ] = False,
) -> None:
    """Search arXiv and append new papers to a YAML file in the labeled-set format."""
    existing = evalset.load(out) if out.exists() else evalset.EvalSet()
    known = {p.paper.id for p in existing.papers}
    found = [p for p in arxiv_source.search(query, max_results) if p.id not in known]
    console.print(f"{len(found)} new papers for {query!r}")

    cascade = build_cascade(Settings.from_env()) if prelabel else None

    spent = 0.0

    async def label(paper):
        nonlocal spent
        result = await cascade.screen(paper)
        spent += result.cost
        return evalset.LabeledPaper(
            paper=paper,
            expected=result.output.flags,
            note=f"draft ({result.model}, {result.output.confidence}): {result.output.reason}",
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            await db.apply_schema(conn)
            return await ingest.run(
                conn,
                settings,
                build_cascade(settings),
                kind,
                days,
                fetch,
                router=build_router(settings),
                digester=build_digester(settings),
                deep=build_deep(settings),
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
    """Route tier 1/2 papers to Zotero: item, collection, tier tag, screen note, PDF."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
                result = await build_cascade(settings).screen(found[0])
                await db.store_screen(conn, result)
                console.print(f"{paper}: screened as {result.tier.value}")
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
            f"  {r['id']} {r['routed_tier']} item={r['zotero_key']} "
            f"pdf={r['zotero_attachment_key']}"
        )
    for pid, err in failures:
        console.print(f"  [red]{pid} failed: {err}[/red]")
    console.print(f"routed {len(rows)}, failed {len(failures)}")


@app.command("migrate-screens")
def migrate_screens() -> None:
    """Rewrite screen JSON stored before 0.3 into the staged shape."""
    settings = Settings.from_env()

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            return await db.migrate_screens(conn)

    console.print(f"rewrote {asyncio.run(go())} rows")


@app.command()
def reconfirm() -> None:
    """Run the confirm stage on tier 1 and 2 papers screened before it existed."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    cascade = build_cascade(settings)

    async def go():
        async with await db.connect(settings.require_database()) as conn:
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
                    cost += result.stages[-1].usage.cost or 0.0
                    if result.tier != prior.tier:
                        changed.append((paper, prior.tier, result.tier))

            await asyncio.gather(*(one(p, r) for p, r in todo))
            return len(todo), changed, cost

    n, changed, cost = asyncio.run(go())
    console.print(f"confirmed {n} papers, {len(changed)} changed tier, cost ${cost:.4f}")
    for paper, before, after in changed:
        console.print(f"  {paper.id} {before.value} -> {after.value}  {paper.title[:70]}")


@app.command()
def digest(
    run_id: Annotated[int | None, typer.Option("--run", help="Run id from the runs table")] = None,
    days: Annotated[
        int | None, typer.Option(help="Instead of a run: papers screened in the last N days")
    ] = None,
    model: Annotated[str | None, typer.Option(help="Override the digest model")] = None,
    store: Annotated[bool, typer.Option(help="Write the digest onto the run row")] = False,
) -> None:
    """Write the prose digest for one run (or a recent window) and print it."""
    if (run_id is None) == (days is None):
        raise SystemExit("give exactly one of --run or --days")
    settings = Settings.from_env()
    digester = build_digester(settings, model)

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            if run_id is not None:
                positives = await db.run_positives(conn, run_id)
            else:
                assert days is not None
                since = datetime.now(UTC) - timedelta(days=days)
                positives = await db.positives_since(conn, since)
            text, usage = await digester.write(positives)
            if store and run_id is not None:
                await db.store_digest(conn, run_id, text, usage.model if usage else None)
            return positives, text, usage

    positives, text, usage = asyncio.run(go())
    print(text)  # raw, unwrapped: the console printer would insert line breaks
    cost = f"${usage.cost:.4f}" if usage and usage.cost is not None else "n/a"
    console.print(f"\n[dim]{len(positives)} papers, {digester.model}, cost {cost}[/dim]")


@app.command()
def deep(
    paper: Annotated[
        str | None, typer.Option(help="One arXiv id, processed even if it was tried before")
    ] = None,
    limit: Annotated[int, typer.Option(help="Pending papers to process at most")] = 20,
) -> None:
    """Deep-process pending tier 1 papers (or one paper) and write the results to Zotero."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    processor = build_deep(settings)
    if processor is None:
        raise SystemExit("LITWATCH_DEEP_URL is not set")

    async def go():
        async with await db.connect(settings.require_database()) as conn:
            await db.apply_schema(conn)
            return await processor.process_pending(conn, paper, limit)

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
    """Row counts per tier."""
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

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(create_app(Settings.from_env()), host=host, port=port, log_level="info")
