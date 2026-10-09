"""Draw papers that triage rejected, for the owner to look at what the gate drops."""

from __future__ import annotations

import random

import psycopg

from litwatch.config import Config
from litwatch.core import Status, TopicKind
from litwatch.feedback.replay import replay
from litwatch.jobs.context import GatewayFactory, default_gateway
from litwatch.store import feedback as feedback_store
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store

NEAR, FAR = "near", "far"


async def sample_rejects(
    conn: psycopg.AsyncConnection,
    config: Config,
    *,
    seed: int,
    pool: int,
    near: int = 70,
    far: int = 30,
    gateway: GatewayFactory = default_gateway,
) -> int:
    """Triage a seeded random pool of stored papers and draw from those it rejected.

    A reject is a paper triage answered no for on every Watch topic. `near` of them come from
    rejects it placed in a Radar field, the nearest neighbours of the Watch topics, and `far`
    from all other rejects. The pool, the strata and the order all follow from the seed and
    the collection. Returns the evaluation run that holds the pool's answers and the sample.
    """
    rng = random.Random(seed)
    rows = await (await conn.execute("SELECT id FROM papers ORDER BY id")).fetchall()
    ids = [r["id"] for r in rows]
    chosen = sorted(rng.sample(ids, min(pool, len(ids))))
    papers = await papers_store.get_many(conn, chosen)
    eval_run_id = await replay(
        conn,
        config,
        papers,
        grade=set(),
        kind="sample",
        note="reject sample",
        params={"seed": seed, "pool": len(chosen), "near": near, "far": far},
        gateway=gateway,
    )
    topics = await topics_store.all(conn, Status.ACTIVE)
    kinds = {t.slug: t.kind for t in topics}
    answers: dict[str, dict[str, bool | None]] = {}
    for row in await feedback_store.results(conn, eval_run_id):
        answers.setdefault(row["paper_id"], {})[row["topic"]] = row["triage_yes"]

    def said(paper_id: str, kind: TopicKind) -> list[bool | None]:
        return [yes for slug, yes in answers[paper_id].items() if kinds[slug] == kind]

    # A paper whose triage call failed was not rejected; it has no answers at all.
    rejects = [p for p in chosen if all(yes is False for yes in said(p, TopicKind.WATCH))]
    in_field = [p for p in rejects if any(said(p, TopicKind.RADAR))]
    elsewhere = [p for p in rejects if not any(said(p, TopicKind.RADAR))]
    drawn = [(p, NEAR) for p in rng.sample(in_field, min(near, len(in_field)))]
    drawn += [(p, FAR) for p in rng.sample(elsewhere, min(far, len(elsewhere)))]
    rng.shuffle(drawn)
    await feedback_store.put_sample(conn, eval_run_id, drawn)
    return eval_run_id
