"""P1's tables (docs/plans/model-spend.md): each run of `litwatch eval briefs` in this directory
against Opus 5.5's answers from N6 (opus.jsonl, select.sql), and the pairs the owner reads.

    python3 evals/2026-10-16-sonnet-briefs/compare.py [PAIR_RUN] > tables.md
"""

import json
import statistics
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
RUNS = ["sonnet-high", "sonnet-medium", "sonnet-high-no-appendix", "argon", "sol", "mimo"]


def load(name: str) -> dict[str, dict]:
    path = HERE / f"{name}.jsonl"
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    return {r["paper_id"]: r for r in rows}  # the last answer of a paper


def agreement(opus: dict[str, dict], run: dict[str, dict]) -> tuple[Counter, Counter, int]:
    weights, lines, n = Counter(), Counter(), 0
    for pid, o in opus.items():
        out = (run.get(pid) or {}).get("output")
        if o["kind"] == "refused" or not out:
            continue
        n += 1
        weights[(o["weight"], out["weight"])] += 1
        lines[(o["overstatement"] is not None, out["overstatement"] is not None)] += 1
    return weights, lines, n


def summary(opus: dict[str, dict]) -> str:
    rows = [
        "| Run | Answered | Refused | Weight as Opus | Overstatement line as Opus | Input tokens (median) | "
        "Output tokens (median) | Cost a pass | Cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in RUNS:
        run = load(name)
        if not run:
            continue
        answered = [r for r in run.values() if r.get("output")]
        refused = [r for r in run.values() if (r.get("error") or "").startswith("refused")]
        weights, lines, n = agreement(opus, run)
        same_w = sum(c for (a, b), c in weights.items() if a == b)
        same_l = sum(c for (a, b), c in lines.items() if a == b)
        cost = sum(r.get("cost") or 0 for r in run.values())
        tokens_in = statistics.median(r["input_tokens"] for r in answered) if answered else 0
        tokens_out = statistics.median(r["output_tokens"] for r in answered) if answered else 0
        rows.append(
            f"| {name} | {len(answered)} of {len(run)} | {len(refused)} | "
            f"{same_w} of {n} | {same_l} of {n} | {tokens_in:,.0f} | {tokens_out:,.0f} | "
            f"${cost / max(len(run), 1):.3f} | ${cost:.2f} |"
        )
    return "\n".join(rows)


def crosstabs(opus: dict[str, dict], name: str) -> str:
    weights, lines, _ = agreement(opus, load(name))
    out = [
        f"**{name}.** Weight, Opus → this run: "
        + ", ".join(f"{a} → {b} {c}" for (a, b), c in sorted(weights.items()))
    ]
    said = {(True, True): "both", (True, False): "Opus only", (False, True): "this run only",
            (False, False): "neither"}  # fmt: skip
    out.append("Overstatement line: " + ", ".join(
        f"{said[k]} {c}" for k, c in sorted(lines.items(), reverse=True)))  # fmt: skip
    return "\n\n".join(out)


def pairs(opus: dict[str, dict], name: str) -> str:
    """The papers where the run and Opus differ on Weight or the overstatement line, then the
    refusals, each with both answers."""
    run = load(name)
    picked = []
    for pid, o in opus.items():
        out = (run.get(pid) or {}).get("output")
        if not out:
            continue
        differs = (
            o["kind"] == "refused"
            or o["weight"] != out["weight"]
            or ((o["overstatement"] is None) != (out["overstatement"] is None))
        )
        if differs:
            picked.append((pid, o, out))
    blocks = []
    for pid, o, out in picked:
        blocks.append(f"### {pid} · {o['kind']} · area {o['area']}\n")
        if o["kind"] == "refused":
            blocks.append("**Opus 5.5.** refused (N6)\n")
        else:
            blocks.append(
                f"**Opus 5.5.** {o['weight']}: {o['weight_reason']}\n\n"
                f"*Overstatement:* {o['overstatement'] or 'none'}\n\n{o['brief']}\n"
            )
        blocks.append(
            f"**{name}.** {out['weight']}: {out['weight_reason']}\n\n"
            f"*Overstatement:* {out['overstatement'] or 'none'}\n\n{out['brief']}\n"
        )
    return "\n".join(blocks)


if __name__ == "__main__":
    opus = {r["id"]: r for r in map(json.loads, (HERE / "opus.jsonl").read_text().splitlines())}
    pair_run = sys.argv[1] if len(sys.argv) > 1 else "sonnet-high"
    print(summary(opus), end="\n\n")
    for name in RUNS[:3]:
        if load(name):
            print(crosstabs(opus, name), end="\n\n")
    print("## Pairs\n")
    print(pairs(opus, pair_run))
