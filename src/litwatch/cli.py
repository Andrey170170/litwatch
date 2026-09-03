"""Command line entry points for phase 1: screen, eval, candidates."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from litwatch import arxiv_source, db, evalset, evaluation, ingest
from litwatch.config import Settings
from litwatch.models import Tier
from litwatch.screen import Cascade, ModelScreener, openrouter_client

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

LABELED = Path("evals/labeled.yaml")
RUNS = Path("evals/runs")


def build_cascade(settings: Settings, rescreen: bool = True) -> Cascade:
    client = openrouter_client(settings.openrouter_api_key)
    primary = ModelScreener(client, settings.screen_model, settings.screen_effort)
    secondary = (
        ModelScreener(client, settings.rescreen_model, settings.rescreen_effort)
        if rescreen
        else None
    )
    return Cascade(primary, secondary)


@app.command()
def screen(
    paper_id: Annotated[str, typer.Argument(help="arXiv id or URL")],
    rescreen: Annotated[bool, typer.Option(help="Apply the second-stage re-screen rule")] = True,
) -> None:
    """Screen one arXiv paper and print the result."""
    settings = Settings.from_env()
    papers = arxiv_source.fetch_by_ids([arxiv_source.id_from_url(paper_id)])
    if not papers:
        raise typer.BadParameter(f"arXiv returned nothing for {paper_id}")
    result = asyncio.run(build_cascade(settings, rescreen).screen(papers[0]))
    console.print(f"[bold]{papers[0].title}[/bold]")
    console.print_json(result.model_dump_json())
    console.print(f"cost: ${result.cost:.4f}")


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


@app.command("eval")
def eval_cmd(
    labeled: Annotated[Path, typer.Option(help="Labeled set YAML")] = LABELED,
    rescreen: Annotated[bool, typer.Option(help="Apply the second-stage re-screen rule")] = True,
    concurrency: int = 8,
) -> None:
    """Run the screener over the labeled set and report per-tier precision and recall."""
    settings = Settings.from_env()
    items = evalset.load(labeled).papers
    report = asyncio.run(
        evaluation.run(build_cascade(settings, rescreen), items, concurrency=concurrency)
    )

    table = Table(title=f"{len(report.scored)} labeled papers, {report.rescreened} re-screened")
    table.add_column("tier")
    table.add_column("P first")
    table.add_column("R first")
    table.add_column("P final")
    table.add_column("R final")
    table.add_column("n")
    for tier in Tier:
        first, final = report.per_tier_first_stage[tier], report.per_tier[tier]
        table.add_row(
            tier.value,
            _fmt(first.precision),
            _fmt(first.recall),
            _fmt(final.precision),
            _fmt(final.recall),
            str(final.tp + final.fn),
        )
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
            return await ingest.run(conn, settings, build_cascade(settings), kind, days, fetch)

    s = asyncio.run(go())
    console.print(
        f"run {s.run_id}: fetched {s.fetched} ({s.inserted} new), screened {s.screened}, "
        f"{s.rescreened} re-screened, {s.failed} failed, cost ${s.cost:.4f}, tiers {s.tiers}"
    )


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
