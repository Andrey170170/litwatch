"""What an evaluation run says about the pipeline, measured against the owner's labels."""

from __future__ import annotations

from typing import Any

import psycopg

from litwatch.core import Grade, LabelValue, TopicKind
from litwatch.store import feedback as feedback_store
from litwatch.store import topics as topics_store


def _surfaced(grade: str | None) -> bool:
    return grade is not None and Grade(grade).at_least(Grade.RELEVANT)


def _share(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None


def _recall(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Four recalls over one denominator: the papers labelled relevant that were replayed."""
    n = len(rows)
    graded = [r for r in rows if r["grade"] is not None]
    return {
        "n": n,
        # The triage model alone, before the keyword audit: the audit must not hide its misses.
        "triage": _share(sum(bool(r["triage_yes"]) for r in rows), n),
        "candidate": _share(sum(_candidate(r) for r in rows), n),
        # Graded relevant or above when forced, whether or not it became a candidate.
        "grading": _share(sum(_surfaced(r["grade"]) for r in graded), len(graded)),
        "end_to_end": _share(
            sum(_candidate(r) and _surfaced(r["grade"]) for r in graded), len(graded)
        ),
        "ungraded": n - len(graded),
        "triage_misses": sorted(r["paper_id"] for r in rows if not r["triage_yes"]),
        "candidate_misses": sorted(r["paper_id"] for r in rows if not _candidate(r)),
        "grading_misses": sorted(r["paper_id"] for r in graded if not _surfaced(r["grade"])),
    }


def _candidate(row: dict[str, Any]) -> bool:
    return bool(row["triage_yes"]) or row["keyword"] is not None


async def metrics(conn: psycopg.AsyncConnection, eval_run_id: int) -> dict[str, Any]:
    """Per Watch topic: recall over papers labelled relevant, precision per label origin,
    the regression set's candidate rates, and how the two graders compare.

    Firm and provisional labels are reported apart, since a provisional one was accepted
    without being read. Precision is never pooled across origins: only a random candidate
    sample was drawn the way production surfaces papers. Unsure labels count nowhere.
    """
    run = await feedback_store.get_eval(conn, eval_run_id)
    if run is None:
        raise ValueError(f"no evaluation run {eval_run_id}")
    kinds = {t.slug: t.kind for t in await topics_store.all(conn, None)}
    results = {
        (r["paper_id"], r["topic"]): r for r in await feedback_store.results(conn, eval_run_id)
    }
    every_label = await feedback_store.labels(conn)
    labels = [label for label in every_label if label.value != LabelValue.UNSURE]
    regression = await feedback_store.regression(conn)
    out: dict[str, Any] = {
        "eval_run": eval_run_id,
        "kind": run["kind"],
        "note": run["note"],
        "models": {
            role: f"{m['provider']}:{m['model']}@{m['effort']}" for role, m in run["models"].items()
        },
        "cost": float(run["cost"]) if run["cost"] is not None else None,
        "calls": (run["counts"] or {}).get("calls"),
        "papers": len({paper_id for paper_id, _ in results}),
        "topics": {},
    }
    for slug in sorted(s for s in run["topics"] if kinds.get(s) == TopicKind.WATCH):
        mine = [
            (label, results.get((label.paper_id, slug))) for label in labels if label.topic == slug
        ]
        replayed = [(label, row) for label, row in mine if row is not None]
        relevant = [(label, row) for label, row in replayed if label.value == LabelValue.RELEVANT]
        precision = {}
        for origin in sorted({label.origin.value for label, _ in replayed}):
            surfaced = [
                label
                for label, row in replayed
                if label.origin.value == origin and _candidate(row) and _surfaced(row["grade"])
            ]
            right = sum(label.value == LabelValue.RELEVANT for label in surfaced)
            precision[origin] = {
                "surfaced": len(surfaced),
                "precision": _share(right, len(surfaced)),
            }
        tiers: dict[str, Any] = {}
        for source in ("legacy", "silver"):
            for tier in (1, 2, 3, 4):
                rows = [
                    results[(r["paper_id"], slug)]
                    for r in regression
                    if r["topic"] == slug
                    and r["source"] == source
                    and r["tier"] == tier
                    and (r["paper_id"], slug) in results
                ]
                if rows:
                    tiers[f"{source}_tier_{tier}"] = {
                        "n": len(rows),
                        "candidates": sum(_candidate(r) for r in rows),
                        "by_triage": sum(bool(r["triage_yes"]) for r in rows),
                        "lost": sorted(r["paper_id"] for r in rows if not _candidate(r))
                        if tier <= 2
                        else None,
                    }
        graded = [r for (_, topic), r in results.items() if topic == slug and r["grade"]]
        topic_rows = [r for (_, topic), r in results.items() if topic == slug]
        out["topics"][slug] = {
            "recall_firm": _recall([row for label, row in relevant if not label.provisional]),
            "recall_provisional": _recall([row for label, row in relevant if label.provisional]),
            "precision_by_origin": precision,
            "regression": tiers,
            "replayed": len(topic_rows),
            "candidates": sum(_candidate(r) for r in topic_rows),
            "candidates_keyword_only": sum(
                _candidate(r) and not r["triage_yes"] for r in topic_rows
            ),
            "graded": len(graded),
            "split": sum(bool(r["split"]) for r in graded),
            # Grading was forced for the paper and came back without two opinions.
            "grading_failed": sum(
                r["grade"] is None and bool(r["error"]) and r["triage_yes"] is not None
                for r in topic_rows
            ),
            "grades": {
                role: {
                    g.value: sum((r[column] or {}).get("grade") == g.value for r in topic_rows)
                    for g in Grade
                }
                for role, column in (("judge", "judge"), ("check", "check_opinion"))
            },
        }
    sample = await feedback_store.sample(conn, eval_run_id)
    if sample:
        out["sample"] = _sample_summary(sample, every_label, kinds)
    return out


def _sample_summary(sample, labels, kinds) -> dict[str, Any]:
    """Per stratum: papers drawn, papers the owner has finished, and Watch papers found."""
    watched = {slug for slug, kind in kinds.items() if kind == TopicKind.WATCH}
    by_paper: dict[str, list] = {}
    for label in labels:
        if label.topic in watched:
            by_paper.setdefault(label.paper_id, []).append(label)
    summary: dict[str, Any] = {}
    for stratum in sorted({row["stratum"] for row in sample}):
        ids = [row["paper_id"] for row in sample if row["stratum"] == stratum]
        found = sorted(
            f"{p}:{label.topic}"
            for p in ids
            for label in by_paper.get(p, [])
            if label.value == LabelValue.RELEVANT
        )
        summary[stratum] = {
            "drawn": len(ids),
            # Finished means a label, unsure included, for every Watch topic.
            "labelled": sum(len(by_paper.get(p, [])) >= len(watched) for p in ids),
            "relevant": found,
            "adjacent": sum(
                label.value == LabelValue.ADJACENT for p in ids for label in by_paper.get(p, [])
            ),
            "unsure": sum(
                label.value == LabelValue.UNSURE for p in ids for label in by_paper.get(p, [])
            ),
        }
    return summary
