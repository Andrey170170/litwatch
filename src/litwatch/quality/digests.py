"""The harness on digests (verification.md, V7): the written digests of a span checked against
the voice rules (`core.digests.voice_problems`) and the picks each depth allows, and the Focus
papers still due after them. It reads what is stored and calls no model."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from litwatch.core.digests import voice_problems

# An Area's daily digest names one or two picks (digests.md); more is a broken answer.
AREA_DAILY_PICKS = 2


def report(
    since: date,
    until: date,
    written: Sequence[Mapping[str, Any]],
    depths: Mapping[str, str],
    still_due: Mapping[str, int],
) -> str:
    """Markdown: per depth and period, how many digests were written and how many broke a rule,
    each breach by digest; then each Focus topic's papers still due."""
    lines = [f"# Digests {since} to {until}", ""]
    breaches: list[str] = []
    tally: dict[tuple[str, str], list[int]] = {}
    for d in written:
        depth = depths.get(d["topic"], "?")
        found = voice_problems(f"{d['gist']}\n{d['text']}", d["titles"])
        picks = d["counts"].get("picks", 0)
        if depth == "area" and d["period"] == "day" and picks > AREA_DAILY_PICKS:
            found.append(f"{picks} picks")
        n = tally.setdefault((depth, d["period"]), [0, 0])
        n[0] += 1
        n[1] += bool(found)
        if found:
            breaches.append(f"- {d['day']} {d['period']} {d['topic']}: {', '.join(found)}")
    lines += ["| depth | period | written | breaking a rule |", "|---|---|---|---|"]
    lines += [f"| {k[0]} | {k[1]} | {v[0]} | {v[1]} |" for k, v in sorted(tally.items())]
    lines += ["", "## Breaches", "", *(breaches or ["none"]), "", "## Focus papers still due", ""]
    lines += [f"- {topic}: {n}" for topic, n in still_due.items()] or ["no Focus topics"]
    return "\n".join(lines) + "\n"
