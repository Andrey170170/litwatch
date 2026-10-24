"""Replay (docs/specs/v1/feedback.md, evaluation; milestone N2c): the labelled and missed papers
run through the current classification, keyword check and grading, under the configured roles
and the taxonomy in the database, and measured per Focus topic and cohort.

A dry run: it calls the same `classification.classify` and `grading.grade` the nightly run does
and writes what they concluded to the evaluation tables alone (`eval_runs`, `eval_labels`,
`eval_results`), plus the run record and call journal that carry its cost. Grading for a Focus
topic runs on every labelled paper of that topic, whatever classification said, so grading is
measured on its own. The labels are frozen with the run, so `report` reads its numbers again
without calls; a run cut short resumes under its id, redoing only what has not concluded or
failed. It runs from the command line (`litwatch eval replay`) or as a job (`jobs.replays`), a
chunk of papers at a time (`work`).

The cohorts, each with its own positives and never pooled (evals/labels/README.md):
`owner` and `provisional` from fit.csv (positive: central or related), `legacy` and `silver`
from regression.csv (positive: tier 1 or 2), and `miss`, the misses reported in the database
(every one positive).
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import json
import random
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg

import litwatch
from litwatch import classification, grading
from litwatch.config import Config, RoleConfig
from litwatch.core import Depth, Fit, Paper, Status, Taxonomy
from litwatch.core.classification import Source, keyword, with_ancestors
from litwatch.core.evaluation import Outcome, Recall, Share
from litwatch.jobs.context import GatewayFactory, ModelRun, default_gateway, model_run
from litwatch.llm.decisions import chat_request
from litwatch.llm.gateway import VALIDATION_VERSION
from litwatch.llm.openrouter import DEFAULT_ROUTING
from litwatch.store import calls
from litwatch.store import evaluation as store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store

KIND = "replay"
# The evaluation labels: in the checkout, and where the image puts them.
LABELS = Path(litwatch.__file__).parents[2] / "evals" / "labels"
SEED = 20261014
COHORTS = ("owner", "provisional", "legacy", "silver", "miss")
ROLES = (classification.ROLE, "judge", "check")  # Focus grading asks both labs
# What one call costs when the journal holds none of the role's model to go by (USD), from the
# first v1's and N1's runs: Jev classifying, Opus judging, the other lab checking.
FALLBACK_COST = {classification.ROLE: 0.0006, "judge": 0.003, "check": 0.0006}


@dataclass(frozen=True)
class Labelled:
    """A cohort's label on a paper for a topic."""

    paper_id: str
    topic: str
    cohort: str
    positive: bool


def load(root: Path) -> list[Labelled]:
    """The labels of fit.csv and regression.csv, an unsure Fit left out."""
    found: list[Labelled] = []
    with (root / "fit.csv").open() as f:
        for r in csv.DictReader(f):
            if r["fit"] != "unsure":
                positive = r["fit"] in (Fit.CENTRAL, Fit.RELATED)
                found.append(Labelled(r["paper_id"], r["topic"], r["cohort"], positive))
    with (root / "regression.csv").open() as f:
        for r in csv.DictReader(f):
            found.append(Labelled(r["paper_id"], r["topic"], r["source"], r["tier"] in ("1", "2")))
    return found


async def misses(conn: psycopg.AsyncConnection) -> list[Labelled]:
    """The misses reported in the database, each positive in cohort `miss`."""
    return [Labelled(p, t, "miss", True) for p, t in await store.misses(conn)]


def focus(tree: Taxonomy) -> list[str]:
    """The topics a replay measures: the active Focus topics."""
    return [t.slug for t in tree.topics if t.depth == Depth.FOCUS and t.status == Status.ACTIVE]


def select(
    labels: list[Labelled],
    topics: list[str],
    *,
    cohorts: list[str] | None = None,
    negatives: dict[str, int] | None = None,
    seed: int = SEED,
) -> list[Labelled]:
    """The labels on `topics` in `cohorts` (all by default). `negatives` caps a cohort's
    negatives at a number drawn with `seed`; positives are never sampled, since every recall
    number is over them."""
    kept = [x for x in labels if x.topic in topics and (cohorts is None or x.cohort in cohorts)]
    for cohort, n in (negatives or {}).items():
        pool = sorted(
            (x for x in kept if x.cohort == cohort and not x.positive),
            key=lambda x: (x.paper_id, x.topic),
        )
        if n < len(pool):
            dropped = set(pool) - set(random.Random(seed).sample(pool, n))
            kept = [x for x in kept if x not in dropped]
    return kept


# A paper and topic no request is ever about: what renders each prompt's fixed parts.
_PROBE = Paper(id="probe", title="", abstract="")


def fingerprint(config: Config, tree: Taxonomy) -> str:
    """Everything that decides what a replay concludes, hashed, so a run resumes only under the
    same: the roles asked, the thresholds, the classes, the routing of memberships to topics
    (every followed topic's depth, status and version), the answer validation, and the requests
    themselves (the classification questions and each Focus topic's grading prompt, rendered for
    an empty paper), whose instructions are code."""
    by_slug = {t.slug: t for t in tree.topics}
    asked = {
        classification.ROLE: chat_request(classification.request(tree, "", "")).key_material(),
        **{
            s: grading.request(_PROBE, by_slug[s], tree.get(s).name).key_material()
            for s in focus(tree)
        },
    }
    material = {
        "roles": {r: _served(config.models.role(r)) for r in ROLES},
        "thresholds": config.classification.model_dump(mode="json"),
        "classes": tree.classes_version,
        "followed": [
            [t.slug, t.depth.value, t.status.value, t.version]
            for t in tree.topics
            if t.status != Status.DRAFT
        ],
        "validation": VALIDATION_VERSION,
        "requests": asked,
        "code": _code(),
    }
    text = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _served(role: RoleConfig) -> dict[str, Any]:
    """A role as it is served: its configuration with OpenRouter's default routing resolved."""
    served = role.model_dump(mode="json")
    if role.provider == "openrouter":
        served["routing"] = list(role.routing or DEFAULT_ROUTING.get(role.model, ()))
    return served


# The code a replay's conclusions come from: routing, keywords, Fit aggregation and the four
# numbers. Its source is in the fingerprint, so a run never resumes across a change to it; the
# journal still returns identical calls free, so starting afresh costs little.
_CODE = ("classification", "grading", "core.classification", "core.grading", "core.evaluation")


def _code() -> str:
    digest = hashlib.sha256()
    for name in _CODE:
        digest.update(Path(importlib.import_module(f"litwatch.{name}").__file__).read_bytes())
    digest.update(Path(__file__).read_bytes())
    return digest.hexdigest()[:12]


@dataclass
class Replay:
    """A replay to run: its labels, the papers they name that the database holds, and what is
    left to do. New until started (`eval_run_id` None)."""

    tree: Taxonomy
    labels: list[Labelled]
    papers: dict[str, Paper]
    left_out: list[str] = field(default_factory=list)  # labelled papers not in the database
    eval_run_id: int | None = None
    classified: set[str] = field(default_factory=set)  # papers classification concluded on
    graded: set[tuple[str, str]] = field(default_factory=set)
    # Papers whose text changed since the run concluded on them: both stages run again.
    stale: set[str] = field(default_factory=set)

    @property
    def pairs(self) -> list[tuple[str, str]]:
        """Each paper and topic to replay, once however many cohorts label it."""
        return list(dict.fromkeys((x.paper_id, x.topic) for x in self.labels))

    @property
    def to_classify(self) -> list[Paper]:
        ids = dict.fromkeys(p for p, _ in self.pairs if p not in self.classified)
        return [self.papers[p] for p in ids]

    @property
    def to_grade(self) -> list[tuple[str, str]]:
        return [x for x in self.pairs if x not in self.graded]

    @property
    def left(self) -> list[str]:
        """The papers with a stage left to conclude, in the run's order."""
        grade = {p for p, _ in self.to_grade}
        ids = dict.fromkeys(p for p, _ in self.pairs)
        return [p for p in ids if p not in self.classified or p in grade]


async def _tree(conn: psycopg.AsyncConnection) -> Taxonomy:
    tree = await taxonomy_store.load(conn)
    if tree is None:
        raise ValueError("no taxonomy yet: `litwatch taxonomy import` the seed first")
    return tree


async def prepare(
    conn: psycopg.AsyncConnection, labels: list[Labelled], *, limit: int | None = None
) -> Replay:
    """A new replay of `labels` (already `select`ed), on the papers the database holds; with
    `limit`, only the first that many of them."""
    tree = await _tree(conn)
    ids = list(dict.fromkeys(x.paper_id for x in labels))
    papers = {p.id: p for p in await papers_store.get_many(conn, ids)}
    left_out = [p for p in ids if p not in papers]
    kept = [p for p in ids if p in papers][:limit]
    chosen = set(kept)
    held = {p: papers[p] for p in kept}
    return Replay(tree, [x for x in labels if x.paper_id in chosen], held, left_out)


async def resume(conn: psycopg.AsyncConnection, config: Config, eval_run_id: int) -> Replay:
    """The replay `eval_run_id` as far as it got. Refuses a run made under another
    `fingerprint`. A paper whose text changed since the run concluded on it is done again."""
    run = await store.get(conn, eval_run_id)
    if run is None or run["kind"] != KIND:
        raise ValueError(f"no replay {eval_run_id}")
    tree = await _tree(conn)
    if (asked := run["params"].get("request")) != fingerprint(config, tree):
        raise ValueError(
            f"replay {eval_run_id} was asked under another configuration or taxonomy "
            f"({asked}); start a new one"
        )
    outcomes = await store.outcomes(conn, eval_run_id)
    labels = [Labelled(o.paper_id, o.topic, o.cohort, o.positive) for o in outcomes]
    ids = list(dict.fromkeys(x.paper_id for x in labels))
    papers = {p.id: p for p in await papers_store.get_many(conn, ids)}
    done = await store.pairs(conn, eval_run_id)
    # A stage concluded on other text than the paper's current one: what the run said about the
    # paper is dropped and both stages run again, so classification and grading read one text.
    stale = {
        r["paper_id"]
        for r in done
        if r["paper_revision"] is not None
        and r["paper_id"] in papers
        and r["paper_revision"] != papers[r["paper_id"]].revision
    }
    unclassified = {r["paper_id"] for r in done if r["classifier"] is None} | stale
    return Replay(
        tree,
        labels,
        papers,
        eval_run_id=eval_run_id,
        classified={p for p in ids if p not in unclassified},
        graded={
            (r["paper_id"], r["topic"])
            for r in done
            if r["fit"] is not None and r["paper_id"] not in stale
        },
        stale=stale,
    )


async def estimate(conn: psycopg.AsyncConnection, config: Config, r: Replay) -> dict[str, float]:
    """What what is left would cost, by step: the latest calls of each role under its current
    configuration (provider, model, effort) in the journal, or `FALLBACK_COST` where it has
    none. Reused replies would cost less."""
    per_call = {}
    for role in ROLES:
        served = config.models.role(role)
        mean = await calls.mean_cost(conn, role, served.provider, served.model, served.effort)
        per_call[role] = FALLBACK_COST[role] if mean is None else mean
    return {
        "classification": len(r.to_classify) * per_call[classification.ROLE],
        "grading": len(r.to_grade) * (per_call["judge"] + per_call["check"]),
    }


async def start(
    conn: psycopg.AsyncConnection,
    config: Config,
    r: Replay,
    params: dict[str, Any],
    note: str | None = None,
) -> int:
    """Record the run and freeze its labels."""
    models = {role: config.models.role(role).model_dump(mode="json") for role in ROLES}
    topics = {t.slug: t.version for t in r.tree.topics if t.slug in focus(r.tree)}
    params = params | {
        "request": fingerprint(config, r.tree),
        "taxonomy": r.tree.classes_version,
        "thresholds": config.classification.model_dump(mode="json"),
        "left_out": r.left_out,
    }
    async with conn.transaction():
        r.eval_run_id = await store.start(conn, KIND, models, topics, params, note)
        await store.put_labels(
            conn, r.eval_run_id, [(x.paper_id, x.topic, x.cohort, x.positive) for x in r.labels]
        )
    return r.eval_run_id


def _concluded(tree: Taxonomy, c: classification.Classification, topic: str) -> dict[str, Any]:
    """What one classification says for a Focus topic: whether the classifier alone, or the
    uncertain band, makes the paper a member there (directly or through a class under it, as
    membership is inherited, whichever topic would grade it), the highest probability in the
    topic's subtree, and the keyword check's hit, which needs no model and stands when the call
    failed."""
    hit = keyword(tree, c.paper.title, c.paper.abstract).get(topic)
    if c.classified is None:
        return {"keyword": hit, "error": c.error or "no answer"}
    ms = c.memberships
    alone = with_ancestors(tree, {m.slug for m in ms if m.source == Source.CLASSIFIER})
    probabilities = c.classified.probabilities
    return {
        "classifier": topic in alone,
        "uncertain": any(m.slug == topic and m.source == Source.UNCERTAIN for m in ms),
        "probability": max(probabilities.get(s, 0.0) for s in tree.subtree(topic)),
        "keyword": hit,
    }


async def run(
    conn: psycopg.AsyncConnection,
    config: Config,
    r: Replay,
    *,
    gateway: GatewayFactory = default_gateway,
) -> dict[str, Any]:
    """Classify the papers and grade the pairs left to do, recording each as it concludes, as a
    run of its own. Returns the run record's counts."""
    assert r.eval_run_id is not None, "start the replay first"
    async with model_run(conn, config, calls.EVAL, gateway=gateway) as mr:
        await work(conn, config, r, mr)
        await store.finish(conn, r.eval_run_id)
        return mr.counts


async def work(
    conn: psycopg.AsyncConnection,
    config: Config,
    r: Replay,
    mr: ModelRun,
    ids: Collection[str] | None = None,
) -> None:
    """Classify and grade what is left of the papers `ids` (all by default) on the run `mr`,
    which the evaluation then carries, recording each stage as it concludes; what a paper whose
    text changed concluded is forgotten first. Under a ceiling, an attempt it cut short is not
    recorded: it is left to do."""
    assert r.eval_run_id is not None, "start the replay first"
    tree, eval_run_id = r.tree, r.eval_run_id
    chosen = set(r.papers) if ids is None else set(ids)
    topics_of: dict[str, list[str]] = {}
    for p, t in r.pairs:
        topics_of.setdefault(p, []).append(t)
    by_slug = {t.slug: t for t in tree.topics}

    def cut() -> bool:
        return mr.gateway.ceiling is not None and mr.gateway.ceiling.reached

    await store.ran_under(conn, eval_run_id, mr.run_id)
    if stale := sorted(r.stale & chosen):
        await store.forget(conn, eval_run_id, stale)
        r.stale -= set(stale)
    thresholds = config.classification.thresholds
    papers = [p for p in r.to_classify if p.id in chosen]
    async with classification.classify(papers, tree, mr.gateway, thresholds) as found:
        async for c in found:
            if c.error is not None and cut():
                continue
            mr.add("classified" if c.error is None else "classification_failed")
            for topic in topics_of[c.paper.id]:
                await store.put_classified(
                    conn, eval_run_id, c.paper, topic, **_concluded(tree, c, topic)
                )
            if c.error is None:
                r.classified.add(c.paper.id)
    due = [
        grading.Due(r.papers[p], by_slug[t], tree.get(t).name)
        for p, t in r.to_grade
        if p in chosen and t in by_slug
    ]
    async with grading.grade(due, mr.gateway, config.grading) as found:
        async for g in found:
            v = g.verdict
            if v is None and cut():
                continue
            mr.add("graded" if v is not None else "grading_failed")
            await store.put_graded(
                conn,
                eval_run_id,
                g.due.paper.id,
                g.due.paper.revision,
                g.due.topic.slug,
                fit=v.fit.value if v else None,
                opinions=v.opinions if v else {},
                split=v.split if v else None,
                error=None if v else (g.error or "no answer"),
            )
            if v is not None:
                r.graded.add((g.due.paper.id, g.due.topic.slug))


# -- the report -----------------------------------------------------------------------------------


def _share(s: Share) -> str:
    return f"{s.value:.2f} ({s.hits}/{s.n})" if s.n else "–"


def _table(head: list[str], rows: list[list[object]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    return out + ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]


def _lost(o: Outcome, row: dict[str, Any] | None) -> str:
    """Which stage let a positive go, and what it said."""
    row = row or {}
    if not o.classified:
        why = "not classified yet"
    elif not o.member:
        p = row.get("probability")
        why = "not a member" + (f" (classifier at {p:.2f})" if p is not None else f": {o.error}")
    else:
        why = "a member"
    if o.graded and not o.surfaced:
        if o.fit is None:
            why += f"; grading failed: {o.grade_error}"
        else:
            fits = [
                f"{role} {(row.get(col) or {}).get('fit', '?')}"
                for role, col in (("judge", "judge"), ("check", "check_opinion"))
            ]
            why += f"; graded {o.fit} ({', '.join(fits)})"
    elif not o.graded:
        why += "; not graded yet"
    return why


def report(
    run: dict[str, Any], recall: list[Recall], outcomes: list[Outcome], rows: list[dict]
) -> str:
    """The run's numbers as markdown: the four recalls per cohort and Focus topic, then every
    positive that did not come through end to end, with the stage that let it go."""
    params = run["params"]
    cost = f"${float(run['cost']):.2f}" if run["cost"] is not None else "unknown"
    models = ", ".join(
        f"{role} {m['provider']}:{m['model']}" + (f"@{m['effort']}" if m.get("effort") else "")
        for role in ROLES
        if (m := run["models"].get(role))
    )
    finished = run["finished_at"].isoformat(timespec="minutes") if run["finished_at"] else "no"
    lines = [
        f"## Replay {run['id']}",
        "",
        f"- Started {run['started_at'].isoformat(timespec='minutes')}, finished: {finished}; "
        f"cost {cost}.",
        f"- Roles: {models}.",
        f"- Taxonomy revision {str(params.get('taxonomy'))[:12]}; Focus topics and versions: "
        + ", ".join(f"{s} {v[:12]}" for s, v in run["topics"].items())
        + ".",
        f"- Thresholds: {params.get('thresholds')}.",
    ]
    if params.get("negatives"):
        lines.append(
            f"- Negatives sampled with seed {params.get('seed')}: {params['negatives']}; "
            "positives all."
        )
    if params.get("limit"):
        lines.append(f"- Limited to the first {params['limit']} papers.")
    if params.get("left_out"):
        lines.append(f"- {len(params['left_out'])} labelled papers not in the database, left out.")
    lines += [
        "",
        "### Recall per Focus topic and cohort",
        "",
        "Over the papers each cohort holds positive (owner and provisional: central or related; "
        "legacy and silver: 0.14 tier 1–2; miss: every report). Classification: the classifier "
        "alone made it a member at threshold. Membership: classifier, uncertain band or keyword "
        "check. Grading: graded related or above, wherever classification put it; a failed "
        "grading is a miss. End to end: a member and graded related or above. Negatives: the "
        "cohort's other papers that came through end to end.",
        "",
    ]
    lines += _table(
        [
            "Cohort",
            "Topic",
            "Positives",
            "Classification",
            "Membership",
            "Grading",
            "End to end",
            "Grading failed",
            "Negatives through",
        ],
        [
            [
                x.cohort,
                x.topic,
                x.positives,
                _share(x.classification),
                _share(x.membership),
                _share(x.grading),
                _share(x.end_to_end),
                x.grading_failed,
                _share(x.negatives),
            ]
            for x in recall
        ],
    )
    by_pair = {(r["paper_id"], r["topic"]): r for r in rows}
    lost = [o for o in outcomes if o.positive and not (o.member and o.surfaced)]
    if lost:
        lines += ["", "### Positives lost end to end", ""]
        lines += _table(
            ["Cohort", "Topic", "Paper", "Title", "Where"],
            [
                [
                    o.cohort,
                    o.topic,
                    o.paper_id,
                    (by_pair.get((o.paper_id, o.topic)) or {}).get("title", "").replace("|", "/"),
                    _lost(o, by_pair.get((o.paper_id, o.topic))),
                ]
                for o in lost
            ],
        )
    return "\n".join(lines) + "\n"
