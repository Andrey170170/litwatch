"""Classifiers against the labels (milestone N1). `run` sorts the evaluation papers with one
candidate and keeps every answer, one JSON line per paper, so a run can be resumed and scored
again at any threshold without new calls. `report` scores the answers per cohort.

The papers: every paper of `fit.csv`, `regression.csv` and the silver sample, and a draw of
0.14's screened papers (seed 20261014), on which candidate volume and 0.14's memberships are
scored without the CoT-heavy bias of the rest.
"""

from __future__ import annotations

import asyncio
import csv
import random
import statistics
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from litwatch import classification
from litwatch.core import Paper
from litwatch.core.classification import (
    DEFAULT_THRESHOLD,
    Classified,
    band,
    direct,
    keyword,
    with_ancestors,
)
from litwatch.core.taxonomy import Depth, Taxonomy
from litwatch.llm.gateway import CallError, Gateway

SEED = 20261014
DRAW = 300
SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7)
FLOORS = (0.1, 0.2, 0.3)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open() as f:
        return list(csv.DictReader(f))


@dataclass
class Labels:
    """The evaluation set, by cohort."""

    # cohort -> Focus class -> papers that are central (or 0.14 tier 1–2) on it
    focus: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    # paper -> (primary, members, unsettled), the two labs' agreement
    silver: dict[str, tuple[str, set[str], set[str]]] = field(default_factory=dict)
    adjudicated: dict[str, str] = field(default_factory=dict)
    # the draw from 0.14's screened papers, with each paper's tier per selection
    draw: dict[str, dict[str, str]] = field(default_factory=dict)
    papers: list[str] = field(default_factory=list)


def load(root: Path) -> Labels:
    labels = Labels()

    def positive(cohort: str, cls: str, pid: str) -> None:
        labels.focus.setdefault(cohort, {}).setdefault(cls, set()).add(pid)

    fit = _rows(root / "fit.csv")
    for r in fit:
        if r["fit"] == "central":
            positive(r["cohort"], r["topic"], r["paper_id"])
    regression = _rows(root / "regression.csv")
    for r in regression:
        if r["tier"] in ("1", "2"):
            positive(f"regression-{r['source']}", r["topic"], r["paper_id"])
    for r in _rows(root / "silver.csv"):
        members, unsettled = set(r["members"].split()), set(r["unsettled"].split())
        labels.silver[r["paper_id"]] = (r["primary"], members, unsettled)
    labels.adjudicated = {r["paper_id"]: r["primary"] for r in _rows(root / "adjudicated.csv")}
    tiers = _rows(root / "tiers-0.14.csv")
    picked = random.Random(SEED).sample(sorted(r["paper_id"] for r in tiers), DRAW)
    by_id = {r["paper_id"]: r for r in tiers}
    labels.draw = {pid: {k: v for k, v in by_id[pid].items() if k != "paper_id"} for pid in picked}
    sample = [r["paper_id"] for r in _rows(root / "silver-sample.csv")]
    every = [r["paper_id"] for r in fit + regression] + sample + picked
    labels.papers = list(dict.fromkeys(every))
    return labels


class Result(BaseModel):
    """One paper's answer from one candidate, or why there is none."""

    paper_id: str
    probabilities: dict[str, float] = {}
    primary: str | None = None
    keywords: dict[str, str] = {}  # the keyword check's hits; no model involved
    cost: float | None = None
    seconds: float = 0.0
    error: str | None = None

    @property
    def classified(self) -> Classified:
        assert self.primary is not None
        return Classified(probabilities=self.probabilities, primary=self.primary)


def results(path: Path) -> dict[str, Result]:
    if not path.exists():
        return {}
    lines = path.read_text().splitlines()
    return {r.paper_id: r for r in (Result.model_validate_json(x) for x in lines if x)}


async def run(
    gateway: Gateway,
    tree: Taxonomy,
    papers: list[Paper],
    out: Path,
    *,
    concurrency: int = 8,
    progress: Callable[[Result], None] | None = None,
) -> list[Result]:
    """Sort every paper not yet in `out`, appending each answer as it arrives."""
    done = results(out)
    gate = asyncio.Semaphore(concurrency)
    found: list[Result] = []

    async def one(paper: Paper) -> None:
        hits = keyword(tree, paper.title, paper.abstract)
        async with gate:
            started = time.monotonic()
            try:
                answer = await gateway.decide(
                    classification.ROLE,
                    classification.request(tree, paper.title, paper.abstract),
                    paper_id=paper.id,
                )
            except CallError as exc:
                result = Result(paper_id=paper.id, keywords=hits, error=str(exc))
            else:
                c = classification.classified(tree, answer.value)
                result = Result(
                    paper_id=paper.id,
                    probabilities={k: round(v, 4) for k, v in c.probabilities.items()},
                    primary=c.primary,
                    keywords=hits,
                    cost=answer.usage.cost,
                )
            result.seconds = round(time.monotonic() - started, 2)
        with out.open("a") as f:
            f.write(result.model_dump_json() + "\n")
        found.append(result)
        if progress:
            progress(result)

    out.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.gather(*(one(p) for p in papers if p.id not in done))
    return found


# -- scoring ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Setting:
    """Where membership is drawn: the default threshold and the uncertain band's floor."""

    threshold: float = DEFAULT_THRESHOLD
    floor: float = 0.2


AT_DEFAULT = Setting()


def members(tree: Taxonomy, r: Result, s: Setting = AT_DEFAULT) -> set[str]:
    """Classes the classifier puts the paper in, inherited ones included."""
    return with_ancestors(tree, direct(tree, r.classified, s.threshold))


def focus_routes(tree: Taxonomy, r: Result, cls: str, s: Setting = AT_DEFAULT) -> tuple[bool, ...]:
    """Whether the paper reaches a Focus class: by the classifier, with the band, with the
    band and the keyword check."""
    by_class = cls in members(tree, r, s)
    by_band = by_class or cls in band(tree, r.classified, s.threshold, s.floor)
    return by_class, by_band, by_band or cls in r.keywords


def precision_recall(judged: list[tuple[bool, bool]]) -> tuple[float | None, float | None]:
    """Over (predicted, labelled) pairs; None where nothing was predicted or labelled."""
    tp = sum(p and t for p, t in judged)
    predicted, held = sum(p for p, _ in judged), sum(t for _, t in judged)
    return (tp / predicted if predicted else None, tp / held if held else None)


def _pr(judged: list[tuple[bool, bool]]) -> str:
    return " / ".join("–" if v is None else f"{v:.2f}" for v in precision_recall(judged))


def _share(hits: int, n: int) -> str:
    return f"{hits}/{n}" if n else "–"


def _pct(hits: int, n: int) -> str:
    return f"{hits / n:.0%} ({hits}/{n})" if n else "–"


def _table(head: list[str], rows: Iterable[Iterable[object]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    return out + ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]


def _top(tree: Taxonomy, slug: str) -> str:
    return [slug, *tree.ancestors(slug)][-1]


def report(tree: Taxonomy, labels: Labels, runs: dict[str, dict[str, Result]]) -> str:
    """The comparison as markdown tables, one section per metric, a column per candidate."""
    names = list(runs)
    ok = {n: {pid: r for pid, r in rs.items() if r.error is None} for n, rs in runs.items()}
    focus = [t.class_ for t in tree.topics if t.depth == Depth.FOCUS]
    lines: list[str] = ["## Runs", ""]

    rows = []
    for n, rs in runs.items():
        costs = [r.cost for r in ok[n].values() if r.cost is not None]
        secs = sorted(r.seconds for r in ok[n].values())
        p90 = secs[int(0.9 * (len(secs) - 1))] if secs else 0
        rows.append([
            n, len(rs), _pct(len(rs) - len(ok[n]), len(rs)),
            f"${1000 * statistics.fmean(costs):.2f}" if costs else "–",
            f"{statistics.fmean(secs):.1f} / {p90:.1f} / {max(secs):.1f}" if secs else "–",
        ])  # fmt: skip
    lines += _table(
        ["Candidate", "Papers", "Failed", "Cost per 1,000", "Seconds: mean / p90 / max"], rows
    )

    lines += ["", "## Focus membership recall", ""]
    lines += [
        "Of the papers each cohort holds central (0.14 and regression: tier 1–2) on a Focus "
        "topic, those that reach its class: classifier at 0.5 / with the uncertain band "
        "[0.2, 0.5) / with the band and the keyword check. Silver: both labs put the paper in "
        "the class. Papers a candidate failed on are left out of its denominator.",
        "",
    ]
    cohorts = dict(labels.focus)
    cohorts["silver"] = {
        cls: {p for p, (_, mem, _) in labels.silver.items() if cls in mem} for cls in focus
    }
    rows = []
    for cohort, by_class in cohorts.items():
        for cls in focus:
            if not (pids := by_class.get(cls)):
                continue
            row: list[object] = [cohort, cls, len(pids)]
            for n in names:
                got = [focus_routes(tree, ok[n][p], cls) for p in pids if p in ok[n]]
                row.append(" / ".join(_share(sum(g[i] for g in got), len(got)) for i in range(3)))
            rows.append(row)
    lines += _table(["Cohort", "Class", "Positives", *names], rows)

    provisional = [(c, p) for c, ps in labels.focus.get("provisional", {}).items() for p in ps]
    heads = [h for n in names for h in (f"{n} volume", f"{n} provisional recall")]

    def volume_and_recall(n: str, s: Setting, routes: range) -> list[str]:
        drawn = [
            [any(focus_routes(tree, ok[n][p], c, s)[i] for c in focus) for i in routes]
            for p in labels.draw
            if p in ok[n]
        ]
        got = [focus_routes(tree, ok[n][p], c, s) for c, p in provisional if p in ok[n]]
        return [
            " / ".join(f"{sum(d[j] for d in drawn) / max(len(drawn), 1):.1%}"
                       for j in range(len(routes))),
            " / ".join(_share(sum(g[i] for g in got), len(got)) for i in routes),
        ]  # fmt: skip

    lines += ["", "## Focus volume on the 0.14 draw, by threshold", ""]
    lines += [
        f"Share of the {len(labels.draw)} drawn papers that reach any Focus class, and recall "
        "on the provisional cohort (0.14 tier 1–2 on the CoT topics, the cohort N1's check is "
        "on): classifier / with the band [0.2, threshold) / with band and keywords.",
        "",
    ]
    rows = [
        [t, *(v for n in names for v in volume_and_recall(n, Setting(t, min(0.2, t)), range(3)))]
        for t in SWEEP
    ]
    lines += _table(["Threshold", *heads], rows)

    lines += ["", "## The uncertain band's floor", ""]
    lines += [
        "At threshold 0.5, with the band and the keyword check: Focus volume on the draw and "
        "provisional recall, by the band's floor.",
        "",
    ]
    rows = [
        [f, *(v for n in names for v in volume_and_recall(n, Setting(0.5, f), range(2, 3)))]
        for f in FLOORS
    ]
    lines += _table(["Floor", *heads], rows)

    lines += ["", "## Followed topics reached on the 0.14 draw", ""]
    lines += [
        "Share of the drawn papers in each followed topic's subtree at 0.5 (Focus: with the band "
        "and the keyword check), which sets how much grading follows; and the mean number of "
        "direct memberships per paper.",
        "",
    ]
    rows = []
    for t in tree.topics:
        row: list[object] = [t.class_, t.depth.value]
        for n in names:
            drawn = [ok[n][p] for p in labels.draw if p in ok[n]]
            if t.depth == Depth.FOCUS:
                hit = sum(focus_routes(tree, r, t.class_)[2] for r in drawn)
            else:
                hit = sum(t.class_ in members(tree, r) for r in drawn)
            row.append(f"{hit / max(len(drawn), 1):.1%}")
        rows.append(row)
    mean = ["direct memberships per paper", ""]
    for n in names:
        drawn = [ok[n][p] for p in labels.draw if p in ok[n]]
        mean.append(f"{statistics.fmean(len(direct(tree, r.classified)) for r in drawn):.1f}")
    lines += _table(["Topic", "Depth", *names], [*rows, mean])

    lines += ["", "## Primary class", "", "Exact class / same top-level class.", ""]
    rows = []
    for cohort, truth in (
        ("silver", {p: v[0] for p, v in labels.silver.items()}),
        ("adjudicated", labels.adjudicated),
    ):
        row = [cohort, len(truth)]
        for n in names:
            got = [(ok[n][p].primary or "", want) for p, want in truth.items() if p in ok[n]]
            exact = sum(a == b for a, b in got)
            top = sum(_top(tree, a) == _top(tree, b) for a, b in got)
            row.append(f"{_pct(exact, len(got))} / {_pct(top, len(got))}")
        rows.append(row)
    lines += _table(["Cohort", "Papers", *names], rows)

    lines += ["", "## Classes on the silver sample (levels 1–2)", ""]
    lines += [
        "Precision / recall at 0.5, inherited memberships included; a class only one lab gave "
        "the paper counts neither way. Classes with at least 3 silver members.",
        "",
    ]
    rows = []
    for c in tree.classes:
        held = [p for p, (_, mem, _) in labels.silver.items() if c.slug in mem]
        if tree.level(c.slug) > 2 or len(held) < 3:
            continue
        row = [c.name, len(held)]
        for n in names:
            judged = [
                (c.slug in members(tree, ok[n][p]), c.slug in mem)
                for p, (_, mem, unsettled) in labels.silver.items()
                if p in ok[n] and c.slug not in unsettled
            ]
            row.append(_pr(judged))
        rows.append(row)
    lines += _table(["Class", "Members", *names], rows)

    lines += ["", "## 0.14's selections on the draw", ""]
    lines += [
        "Precision / recall at 0.5 against 0.14's tiers: tier 1–2 a member, tier 4 not, tier 3 "
        "and unscreened left out.",
        "",
    ]
    rows = []
    for cls in next(iter(labels.draw.values()), {}):
        held = [p for p, t in labels.draw.items() if t[cls] in ("1", "2")]
        if not held:
            continue
        row = [cls, len(held)]
        for n in names:
            judged = [
                (cls in members(tree, ok[n][p]), t[cls] in ("1", "2"))
                for p, t in labels.draw.items()
                if p in ok[n] and t[cls] in ("1", "2", "4")
            ]
            row.append(_pr(judged))
        rows.append(row)
    lines += _table(["Selection", "Members", *names], rows)
    return "\n".join(lines) + "\n"
