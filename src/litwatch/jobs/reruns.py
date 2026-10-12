"""Re-runs the owner asks for after editing a Watch topic (spec §3, "On demand"). Nothing
retroactive runs by itself; each scope shows its paper count and estimated cost first.

- `evidence`: the topic's labelled papers and misses through the current pipeline, as a dry
  run: whether the edit helped.
- `candidates`: the topic's existing candidates, re-graded for real.
- `keywords`: papers matching the topic's keywords anywhere in the collection, triaged and
  graded for real.
- `range`: papers published in a date range, triaged and graded for real.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Literal

import psycopg

from litwatch import watch
from litwatch.config import Config
from litwatch.core import Topic
from litwatch.feedback import replay
from litwatch.jobs import watch as watch_jobs
from litwatch.jobs.context import GatewayFactory, default_gateway, model_run
from litwatch.jobs.lock import run_lock
from litwatch.store import papers as papers_store
from litwatch.store import quality
from litwatch.store import topics as topics_store

log = logging.getLogger(__name__)

Scope = Literal["evidence", "candidates", "keywords", "range"]
SCOPES: tuple[Scope, ...] = ("evidence", "candidates", "keywords", "range")
# Share of triaged papers that become candidates for a Watch topic (spec §3, volumes).
CANDIDATE_SHARE = 0.05


async def papers_for(
    conn: psycopg.AsyncConnection,
    topic: Topic,
    scope: Scope,
    since: date | None = None,
    until: date | None = None,
) -> list[str]:
    if scope == "evidence":
        return await quality.evidence_papers(conn, topic.slug)
    if scope == "candidates":
        return await quality.candidate_papers(conn, topic.slug)
    if scope == "keywords":
        return await quality.keyword_papers(conn, topic.keywords)
    if since is None or until is None:
        raise ValueError("a date range needs both ends")
    return await quality.papers_between(conn, since, until)


async def estimate(
    conn: psycopg.AsyncConnection,
    topic: Topic,
    scope: Scope,
    since: date | None = None,
    until: date | None = None,
) -> dict[str, Any]:
    """Paper count and estimated cost: calls by role times each role's recent average cost."""
    n = len(await papers_for(conn, topic, scope, since, until))
    graded = {
        "evidence": n,
        "candidates": n,
        "keywords": n,  # every keyword match becomes a candidate
        "range": round(n * CANDIDATE_SHARE),
    }[scope]
    triaged = 0 if scope == "candidates" else n
    per_call = await quality.cost_per_call(conn)
    cost = triaged * per_call.get("triage", 0.0) + graded * (
        per_call.get("judge", 0.0) + per_call.get("check", 0.0)
    )
    return {"scope": scope, "papers": n, "cost": round(cost, 2), "known": bool(per_call)}


async def run(
    conn: psycopg.AsyncConnection,
    config: Config,
    slug: str,
    scope: Scope,
    since: date | None = None,
    until: date | None = None,
    gateway: GatewayFactory = default_gateway,
) -> dict[str, Any]:
    """Carry out a re-run. Returns the run's or the evaluation's id and counts."""
    topic = await topics_store.get(conn, slug)
    if topic is None:
        raise ValueError(f"no topic {slug!r}")
    ids = await papers_for(conn, topic, scope, since, until)
    papers = await papers_store.get_many(conn, ids)
    if scope == "evidence":
        eval_run_id = await replay(
            conn, config, papers, kind="rerun", params={"topic": slug}, gateway=gateway
        )
        return {"eval_run": eval_run_id, "papers": len(papers)}
    async with run_lock(conn), model_run(conn, config, f"rerun:{scope}", gateway=gateway) as r:
        r.counts["topic"] = slug
        if scope == "candidates":
            async for graded in watch.grade_many(
                [(p, topic) for p in papers], r.gateway, config.limits.grading
            ):
                made = await watch.record(conn, graded)
                r.add("graded" if made else "grading_failed")
        else:
            await watch_jobs.triage(conn, config, r, papers=papers)
            await watch_jobs.grade(conn, config, r)
        return {"run": r.run_id, "papers": len(papers), **r.counts}
