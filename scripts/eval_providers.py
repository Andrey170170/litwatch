"""Replay frozen screening cases on one model through each candidate provider; evidence only.

Input: JSON {selections: [...], cases: [{paper: {...}, rescreened: [field, ...]}]}, where
`rescreened` names the selections (output field names) the stage judged. Each case is sent
to every provider alone (no fallbacks), so tiers, reasoning tokens, cost and latency compare
like for like. Agreement is measured against the per-verdict majority across providers.
Writes JSONL; stops starting calls once the reported spend reaches the budget.
"""

import argparse
import asyncio
import collections
import json
import statistics
import time
from pathlib import Path

from litwatch.config import Settings
from litwatch.models import Paper
from litwatch.screen import ModelScreener, openrouter_client
from litwatch.selection import Selection


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cases", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--model", default="z-ai/glm-5.3")
    p.add_argument("--effort", default="high")
    p.add_argument("--providers", required=True, help="comma-separated provider slugs")
    p.add_argument("--max-completion-tokens", type=int, default=8192)
    p.add_argument("--budget", type=float, default=0.75)
    p.add_argument("--concurrency", type=int, default=6)
    args = p.parse_args()
    data = json.loads(args.cases.read_text())
    selections = {
        s["slug"].replace("-", "_"): Selection.model_validate(s) for s in data["selections"]
    }
    client = openrouter_client(Settings.from_env().openrouter_api_key)
    spent, sem, rows = 0.0, asyncio.Semaphore(args.concurrency), []

    async def one(provider: str, case: dict) -> None:
        nonlocal spent
        async with sem:
            if spent >= args.budget:
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
                spent += usage.cost or 0
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

    providers = [x.strip() for x in args.providers.split(",") if x.strip()]
    await asyncio.gather(*(one(pr, c) for c in data["cases"] for pr in providers))
    args.output.write_text("".join(json.dumps(r) + "\n" for r in rows))

    votes = collections.defaultdict(collections.Counter)
    for r in rows:
        for slug, tier in r.get("tiers", {}).items():
            votes[(r["paper"], slug)][tier] += 1
    majority = {k: c.most_common(1)[0][0] for k, c in votes.items()}
    print(f"spent ${spent:.4f}")
    print("provider        ok/err  agree  reason_p50  cost/call  p50_s  p90_s  served")
    for pr in providers:
        mine = [r for r in rows if r["provider"] == pr]
        ok = [r for r in mine if "tiers" in r]
        pairs = [(t, majority[(r["paper"], s)]) for r in ok for s, t in r["tiers"].items()]
        agree = sum(a == b for a, b in pairs) / len(pairs) if pairs else float("nan")
        secs = sorted(r["seconds"] for r in ok) or [float("nan")]
        reason = statistics.median([r["reasoning"] or 0 for r in ok]) if ok else float("nan")
        cost = statistics.mean([r["cost"] or 0 for r in ok]) if ok else float("nan")
        served = collections.Counter(r["served"] for r in ok).most_common(2)
        print(
            f"{pr:15} {len(ok):2}/{len(mine) - len(ok):<3}  {agree:.2f}  {reason:10.0f}  "
            f"{cost:.5f}  {secs[len(secs) // 2]:5.1f}  {secs[int(len(secs) * 0.9)]:5.1f}  {served}"
        )


if __name__ == "__main__":
    asyncio.run(main())
