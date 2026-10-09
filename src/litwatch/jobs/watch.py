"""The screening and grading jobs: triage with the keyword audit, then two-lab grading."""

from __future__ import annotations

import logging

import psycopg

from litwatch import screening, watch
from litwatch.config import Config
from litwatch.core import Paper, Status, TopicKind
from litwatch.jobs.context import ModelRun
from litwatch.store import topics as topics_store
from litwatch.store import triage as triage_store

log = logging.getLogger(__name__)


async def triage(
    conn: psycopg.AsyncConnection,
    config: Config,
    run: ModelRun,
    papers: list[Paper] | None = None,
    limit: int | None = None,
) -> None:
    """Triage the given papers, or every untriaged one, and run the keyword audit on them.

    Each paper's outcome is stored as it arrives, so an interrupted run keeps what it paid for.
    """
    topics = await topics_store.all(conn, Status.ACTIVE)
    if papers is None:
        papers = await triage_store.untriaged(conn, limit)
    if not topics or not papers:
        return
    keywords = screening.audit(papers, topics)
    log.info("run %s triage: %s papers, %s topics", run.run_id, len(papers), len(topics))
    async for triaged in screening.triage(papers, topics, run.gateway, config.limits.triage):
        made = await screening.record(conn, triaged, keywords.get(triaged.paper.id, {}), topics)
        run.add("triage_failed" if triaged.error else "triaged")
        run.add("candidates", len(made))
        run.add("candidates_keyword_only", sum(not c.by_triage for c in made))
    log.info("run %s triage done: %s", run.run_id, run.counts)


async def grade(conn: psycopg.AsyncConnection, config: Config, run: ModelRun) -> None:
    """Grade every pending candidate of an active Watch topic.

    Grading uses the topic as it is now: an edit applies from the next run, also to a
    candidate that was put forward under the earlier wording and not yet graded.
    """
    topics = {
        t.slug: t for t in await topics_store.all(conn, Status.ACTIVE) if t.kind == TopicKind.WATCH
    }
    pending = await triage_store.pending_candidates(conn, config.limits.grading_attempts)
    pairs = [(paper, topics[c.topic]) for c, paper in pending if c.topic in topics]
    made_under = {(c.paper_id, c.topic): c.topic_revision for c, _ in pending}
    if not pairs:
        return
    log.info("run %s grading: %s candidates", run.run_id, len(pairs))
    async for graded in watch.grade_many(pairs, run.gateway, config.limits.grading):
        revision = made_under[graded.paper.id, graded.topic.slug]
        verdict = await watch.record(conn, graded, revision)
        if verdict is None:
            run.add("grading_failed")
            log.warning("%s %s: no verdict (%s)", graded.paper.id, graded.topic.slug, graded.error)
            continue
        run.add("graded")
        run.add(f"grade_{verdict.grade.value}")
        run.add("split", int(verdict.split))
    log.info("run %s grading done: %s", run.run_id, run.counts)
