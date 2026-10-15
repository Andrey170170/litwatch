"""The tables of `evals/2026-10-15-tuning.md`, from the two-week run's data in this directory.

    uv run python evals/2026-10-15-tuning/tuning.py

`calls.csv` is the run's call journal by step, submission day and outcome; `verdicts.csv.gz` every
verdict with its Fit, the paper's Weight, the effective grade and the topic page's rarity (and, for
a Focus topic, the membership that brought the paper in); `papers.csv.gz` every paper with its
analysis and mixed-list rarity. All three were read from the scratch database `n2c_run` on
2026-10-15. The grade and rarity rules are litwatch's own (`litwatch.core.grading`), so a
counterfactual offset is the shipped code with one number changed.
"""

from __future__ import annotations

import csv
import gzip
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from litwatch.core.analysis import Weight
from litwatch.core.grading import DEPTH_OFFSETS, Fit, Rarity, grade, mixed_rarity
from litwatch.core.taxonomy import Depth

HERE = Path(__file__).parent
LABELS = HERE.parent / "labels"
FITS = [f.value for f in Fit]
WEIGHTS = [w.value for w in Weight]
SELECTIONS = [
    "cot-faithfulness",
    "cot-monitoring",
    "interpretability",
    "alignment-and-safety",
    "reasoning-and-rl",
    "agents",
    "efficiency-and-architecture",
]


def rows(name: str) -> list[dict[str, str]]:
    path = HERE / name
    with gzip.open(path, "rt") if name.endswith(".gz") else open(path) as f:
        return list(csv.DictReader(f))


def night(day: str) -> str:
    """The night a paper is processed: its submission day, Saturday and Sunday on Monday."""
    d = date.fromisoformat(day)
    return (d + timedelta(days={5: 2, 6: 1}.get(d.weekday(), 0))).isoformat()


def table(head: list[str], body: list[list[object]]) -> None:
    print("| " + " | ".join(head) + " |")
    print("|" + " --- |" * len(head))
    for r in body:
        print("| " + " | ".join(str(x) for x in r) + " |")
    print()


calls, verdicts, papers = rows("calls.csv"), rows("verdicts.csv.gz"), rows("papers.csv.gz")
with open(LABELS / "tiers-0.14.csv") as f:
    tiers = {r["paper_id"]: r for r in csv.DictReader(f)}
by_paper = defaultdict(list)
for v in verdicts:
    by_paper[v["paper_id"]].append(v)
submitted = {p["paper_id"]: p["submitted"] for p in papers}
weight = {p["paper_id"]: p["weight"] for p in papers}
nights = sorted({night(d) for d in submitted.values()})
per_night = Counter(night(d) for d in submitted.values())
n_papers = len(papers)

# The stored grades are the table's: the data is what the rules make of it.
assert all(
    grade(Fit(v["fit"]), Weight(v["weight"]) if v["weight"] else None) == v["grade"]
    for v in verdicts
)

print("## Cost by step\n")
step = defaultdict(lambda: [0, 0, 0.0])
for c in calls:
    s = step[c["operation"]]
    s[0] += int(c["calls"])
    s[1] += int(c["calls"]) if c["failed"] == "t" else 0
    s[2] += float(c["cost"])
table(
    ["Step", "Calls", "Failed", "Cost", "Per 1,000 papers"],
    [
        [k, v[0], v[1], f"${v[2]:.2f}", f"${v[2] / n_papers * 1000:.2f}"]
        for k, v in sorted(step.items())
    ]
    + [
        [
            "all",
            sum(v[0] for v in step.values()),
            sum(v[1] for v in step.values()),
            f"${sum(v[2] for v in step.values()):.2f}",
            f"${sum(v[2] for v in step.values()) / n_papers * 1000:.2f}",
        ]
    ],
)

print("## Cost by night\n")
cost = defaultdict(Counter)
for c in calls:
    cost[night(c["submitted"])][c["operation"]] += float(c["cost"])
table(
    ["Night", "Papers", "All", "classify", "analyse", "judge + check", "area_check + field_grade"],
    [
        [
            n,
            per_night[n],
            f"${sum(cost[n].values()):.2f}",
            f"${cost[n]['classify']:.2f}",
            f"${cost[n]['analyse']:.2f}",
            f"${cost[n]['judge'] + cost[n]['check']:.2f}",
            f"${cost[n]['area_check'] + cost[n]['field_grade']:.2f}",
        ]
        for n in nights
    ],
)

print("## Focus gradings by the membership that brought the paper in\n")
route = Counter((v["route"], v["fit"] in ("central", "related")) for v in verdicts if v["route"])
table(
    ["Route", "Gradings", "Related or above", "Per 1,000 papers"],
    [
        [
            r,
            route[(r, True)] + route[(r, False)],
            route[(r, True)],
            round((route[(r, True)] + route[(r, False)]) / n_papers * 1000),
        ]
        for r in ("classifier", "band", "keyword")
    ],
)

print("## Weight\n")
w_all = Counter(weight.values())
print(", ".join(f"{w or 'none'} {w_all[w]} ({w_all[w] / n_papers:.1%})" for w in WEIGHTS + [""]))
best: dict[str, int] = {}
for pid in submitted:
    ts = [int(tiers[pid][s]) for s in SELECTIONS if pid in tiers and tiers[pid][s]]
    if ts:
        best[pid] = min(ts)
by_tier = defaultdict(Counter)
for pid, t in best.items():
    by_tier[t][weight[pid]] += 1
print()
table(
    ["0.14's best tier", "Papers", "landmark", "solid", "incremental"],
    [
        [
            t,
            sum(c.values()),
            c["landmark"],
            f"{c['solid'] / sum(c.values()):.0%}",
            f"{c['incremental'] / sum(c.values()):.0%}",
        ]
        for t, c in sorted(by_tier.items())
    ],
)

print("## Fit × Weight per topic, with 0.14's tier on the same selection\n")
for v in verdicts:
    v["tier"] = tiers.get(v["paper_id"], {}).get(v["topic"], "")
for topic in sorted(
    {v["topic"] for v in verdicts},
    key=lambda t: (
        [d.value for d in Depth].index(next(v["depth"] for v in verdicts if v["topic"] == t)),
        t,
    ),
):
    vs = [v for v in verdicts if v["topic"] == topic]
    has_tiers = topic in SELECTIONS
    print(f"### {topic} ({vs[0]['depth']}), {len(vs)} verdicts\n")
    cell = Counter((v["fit"], v["weight"]) for v in vs)
    tier = defaultdict(Counter)
    for v in vs:
        tier[(v["fit"], v["weight"])][v["tier"] or "–"] += 1
    body = []
    for f in FITS:
        row = [f]
        for w in [*WEIGHTS, ""]:
            n = cell[(f, w)]
            t = tier[(f, w)]
            row.append(f"{n} ({t['1']}/{t['2']}/{t['3']}/{t['4']})" if has_tiers and n else n)
        body.append(row)
    table(["Fit \\ Weight", *WEIGHTS, "no analysis (graded as solid)"], body)
    if has_tiers:
        print(
            "Cells: verdicts (0.14's tier 1/2/3/4 on the same selection; the rest 0.14 did not "
            "screen there).\n"
        )

print("## Must reads per night\n")
body = []
for topic in sorted({v["topic"] for v in verdicts}):
    vs = [v for v in verdicts if v["topic"] == topic]
    mr = Counter(night(submitted[v["paper_id"]]) for v in vs if v["grade"] == "must_read")
    graded = Counter(night(submitted[v["paper_id"]]) for v in vs)
    weekdays = [mr[n] for n in nights if date.fromisoformat(n).weekday() != 0 and n != nights[-1]]
    mondays = [mr[n] for n in nights if date.fromisoformat(n).weekday() == 0]
    body.append(
        [
            topic,
            vs[0]["depth"],
            statistics.median(graded[n] for n in nights),
            f"{statistics.median(weekdays):g} ({min(weekdays)}–{max(weekdays)})",
            ", ".join(map(str, mondays)),
            f"{sum(mr.values()) / len(vs):.0%}",
        ]
    )
table(
    [
        "Topic",
        "Depth",
        "Graded a night (median)",
        "Must reads, Tue–Fri (range)",
        "Mondays",
        "Must read share",
    ],
    body,
)

print("## Full-text passes the run's grades would queue\n")
brief, recheck = defaultdict(set), defaultdict(set)
for v in verdicts:
    n = night(submitted[v["paper_id"]])
    if v["depth"] == "focus" and v["fit"] in ("central", "related"):
        brief[n].add(v["paper_id"])
    if v["depth"] == "area" and v["grade"] == "must_read":
        recheck[n].add(v["paper_id"])
table(
    ["Night", "Brief (Focus central or related)", "Weight re-check (Area must read)", "Papers"],
    [[n, len(brief[n]), len(recheck[n]), len(brief[n] | recheck[n])] for n in nights],
)

print("## Mixed-list rarity under the depth offsets\n")


def levels(offsets: dict[Depth, int]) -> dict[str, Rarity]:
    saved = dict(DEPTH_OFFSETS)
    DEPTH_OFFSETS.update(offsets)
    try:
        return {
            pid: mixed_rarity(
                [(v["topic"], Depth(v["depth"]), Rarity(v["topic_rarity"])) for v in by_paper[pid]],
                Weight(weight[pid]) if weight[pid] else None,
            )[0]
            for pid in submitted
        }
    finally:
        DEPTH_OFFSETS.clear()
        DEPTH_OFFSETS.update(saved)


current = levels({})
assert all(current[p["paper_id"]] == p["mixed_rarity"] for p in papers)  # as stored
body = []
for name, off in [
    ("current: Focus 0, Area 0, Field 1", {}),
    ("Field 2", {Depth.FIELD: 2}),
    ("Area 1", {Depth.AREA: 1}),
    ("Area 1, Field 2", {Depth.AREA: 1, Depth.FIELD: 2}),
]:
    lv = levels(off)
    share = Counter(lv.values())
    rare_up = []
    for n in nights:
        day = [pid for pid in submitted if night(submitted[pid]) == n]
        rare_up.append(sum(lv[p].level >= Rarity.RARE.level for p in day) / len(day))
    body.append(
        [
            name,
            *(f"{share[r] / n_papers:.1%}" for r in Rarity),
            f"{statistics.median(rare_up):.0%} ({min(rare_up):.0%}–{max(rare_up):.0%})",
        ]
    )
table(["Offsets", *[r.value for r in Rarity], "Rare or above a night, median (range)"], body)

print("## What Field offset 2 takes below rare\n")
field2 = levels({Depth.FIELD: 2})
up = [p for p in submitted if current[p].level >= Rarity.RARE.level]
moved = [p for p in up if field2[p].level < Rarity.RARE.level]
print(f"{len(moved)} of the {len(up)} papers at rare or above; {len(up) - len(moved)} stay.\n")
