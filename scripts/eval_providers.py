"""Replay frozen screening cases on one model through each candidate provider; evidence only.

Input: JSON {selections: [...], cases: [{paper: {...}, rescreened: [field, ...]}]}, where
`rescreened` names the selections (output field names) the stage judged. Each case is sent
to every provider alone (no fallbacks), so tiers, reasoning tokens, cost and latency compare
like for like. Writes JSONL; stops starting calls once the reported spend reaches the
budget. Rejected responses count toward spend; calls without a response are unknown cost.

Agreement scores each provider against the other providers only: the reference for a
verdict is the most common tier among the others that answered it, and a verdict whose
reference is tied is left out of that provider's denominator. `--not-reference` keeps
providers out of everyone's reference. `--report-only` recomputes from an existing JSONL.
"""

import argparse
import asyncio
import collections
import itertools
import json
import statistics
import time
from pathlib import Path

from litwatch.calls import current_journal
from litwatch.config import Settings
from litwatch.models import Paper, Usage
from litwatch.screen import ModelScreener, openrouter_client
from litwatch.selection import Selection


class MemoryJournal:
    """The journal surface `recorded_completion` uses, kept in memory for spend accounting."""

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.costs: dict[int, float | None] = {}

    async def cached(self, key):
        return None

    async def start(self, operation, model, key, paper_id) -> int:
        call_id = next(self.ids)
        self.costs[call_id] = None
        return call_id

    async def record(self, call_id: int, usage: Usage) -> None:
        self.costs[call_id] = usage.cost or 0.0

    async def accept(self, call_id, output) -> None:
        pass

    async def reject(self, call_id, error) -> None:
        pass

    @property
    def spent(self) -> float:
        return sum(c for c in self.costs.values() if c is not None)

    @property
    def unknown(self) -> int:
        return sum(c is None for c in self.costs.values())


def report(rows: list[dict], providers: list[str], not_reference: set[str]) -> None:
    answers: dict[tuple, dict[str, int]] = collections.defaultdict(dict)
    for r in rows:
        for slug, tier in r.get("tiers", {}).items():
            answers[(r["paper"], slug)][r["provider"]] = tier
    print("provider        ok/err  agree       tied  reason_p50  cost/call  p50_s  p90_s  served")
    for pr in providers:
        mine = [r for r in rows if r["provider"] == pr]
        ok = [r for r in mine if "tiers" in r]
        agreed = scored = tied = 0
        for r in ok:
            for slug, tier in r["tiers"].items():
                others = collections.Counter(
                    t
                    for p, t in answers[(r["paper"], slug)].items()
                    if p != pr and p not in not_reference
                ).most_common()
                if not others:
                    continue
                if len(others) > 1 and others[0][1] == others[1][1]:
                    tied += 1
                    continue
                scored += 1
                agreed += tier == others[0][0]
        secs = sorted(r["seconds"] for r in ok) or [float("nan")]
        reason = statistics.median([r["reasoning"] or 0 for r in ok]) if ok else float("nan")
        cost = statistics.mean([r["cost"] or 0 for r in ok]) if ok else float("nan")
        served = collections.Counter(r["served"] for r in ok).most_common(2)
        print(
            f"{pr:15} {len(ok):2}/{len(mine) - len(ok):<3}  {agreed:3}/{scored:<3}  {tied:5}  "
            f"{reason:10.0f}  {cost:.5f}  {secs[len(secs) // 2]:5.1f}  "
            f"{secs[int(len(secs) * 0.9)]:5.1f}  {served}"
        )


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cases", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--model", default="z-ai/glm-5.3")
    p.add_argument("--effort", default="high")
    p.add_argument("--providers", required=True, help="comma-separated provider slugs")
    p.add_argument("--not-reference", default="", help="comma-separated provider slugs")
    p.add_argument("--max-completion-tokens", type=int, default=8192)
    p.add_argument("--budget", type=float, default=0.75)
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()
    providers = [x.strip() for x in args.providers.split(",") if x.strip()]
    not_reference = {x.strip() for x in args.not_reference.split(",") if x.strip()}
    if args.report_only:
        rows = [json.loads(line) for line in args.output.read_text().splitlines() if line]
        report(rows, providers, not_reference)
        return

    data = json.loads(args.cases.read_text())
    selections = {
        s["slug"].replace("-", "_"): Selection.model_validate(s) for s in data["selections"]
    }
    client = openrouter_client(Settings.from_env().openrouter_api_key)
    journal = MemoryJournal()
    current_journal.set(journal)
    sem, rows = asyncio.Semaphore(args.concurrency), []

    async def one(provider: str, case: dict) -> None:
        async with sem:
            if journal.spent >= args.budget:
                return
            paper = Paper.model_validate(case["paper"])
            screener = ModelScreener(
                client,
                args.model,
                args.effort,
                max_completion_tokens=args.max_completion_tokens,
                provider_order=(provider,),
                timeout_seconds=180,
                operation="rescreen",
            )
            t0 = time.monotonic()
            row: dict = {"provider": provider, "paper": paper.id}
            try:
                verdicts, usage = await screener.screen(
                    paper, [selections[f] for f in case["rescreened"]]
                )
                row |= {
                    "tiers": {k: v.tier for k, v in verdicts.items()},
                    "served": usage.provider,
                    "cost": usage.cost,
                    "reasoning": usage.reasoning_tokens,
                    "completion": usage.completion_tokens,
                }
            except Exception as exc:  # noqa: BLE001 - recorded as evidence
                row["error"] = f"{type(exc).__name__}: {exc}"[:300]
            row["seconds"] = round(time.monotonic() - t0, 2)
            rows.append(row)

    await asyncio.gather(*(one(pr, c) for c in data["cases"] for pr in providers))
    args.output.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"spent ${journal.spent:.4f}; {journal.unknown} calls with unknown cost")
    report(rows, providers, not_reference)


if __name__ == "__main__":
    asyncio.run(main())
