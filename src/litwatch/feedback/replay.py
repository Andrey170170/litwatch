"""Run papers through the current triage, keyword audit and grading without recording them."""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from litwatch import screening, watch
from litwatch.config import ROLES, Config
from litwatch.core import LabelValue, Paper, Status, Topic, TopicKind
from litwatch.jobs.context import GatewayFactory, default_gateway, model_run
from litwatch.store import feedback as feedback_store
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store

log = logging.getLogger(__name__)


async def replay_set(conn: psycopg.AsyncConnection) -> tuple[list[Paper], set[str]]:
    """The papers the evaluation runs on, and the ids among them to grade.

    Every labelled paper is graded for every Watch topic whether or not it becomes a
    candidate, which keeps a triage miss from being counted as a grading error. Regression
    papers that carry no label only go through triage and the audit: their requirement is
    about becoming a candidate.
    """
    labelled = {
        label.paper_id
        for label in await feedback_store.labels(conn)
        if label.value != LabelValue.UNSURE
    }
    regression = {row["paper_id"] for row in await feedback_store.regression(conn)}
    papers = await papers_store.get_many(conn, sorted(labelled | regression))
    return papers, labelled


async def replay(
    conn: psycopg.AsyncConnection,
    config: Config,
    papers: list[Paper],
    *,
    grade: set[str] | None = None,
    kind: str = "replay",
    note: str | None = None,
    params: dict[str, Any] | None = None,
    gateway: GatewayFactory = default_gateway,
    draft: Topic | None = None,
    only: set[str] | None = None,
) -> int:
    """Triage and audit `papers` under `config`, then grade those whose id is in `grade` for
    every Watch topic (all of them when `grade` is None). Returns the evaluation run's id.

    With a `draft`, that topic as edited but not saved replaces the stored one, and only it is
    graded: a preview of the edit. One result row per paper and topic holds what each stage
    said. `only` limits grading to the named Watch topics.
    """
    topics = await topics_store.all(conn, Status.ACTIVE)
    if draft is not None:
        draft = draft.model_copy(update={"revision": 0})  # 0: never saved
        topics = [t for t in topics if t.slug != draft.slug] + [draft]
    watched = [t for t in topics if t.kind == TopicKind.WATCH]
    if draft is not None:
        watched = [draft] if draft.kind == TopicKind.WATCH else []
        params = (params or {}) | {"draft": draft.model_dump(mode="json")}
    if only is not None:
        watched = [t for t in watched if t.slug in only]
    async with model_run(conn, config, "eval", gateway=gateway) as run:
        eval_run_id = await feedback_store.start_eval(
            conn,
            kind,
            run.run_id,
            {role: config.models.role(role).model_dump(mode="json") for role in ROLES},
            {t.slug: t.revision for t in topics},
            params or {},
            note,
        )
        try:
            keywords = screening.audit(papers, topics)
            log.info("eval %s: triage of %s papers", eval_run_id, len(papers))
            async for triaged in screening.triage(
                papers, topics, run.gateway, config.limits.triage
            ):
                paper = triaged.paper
                run.add("triage_failed" if triaged.error else "triaged")
                async with conn.transaction():
                    for topic in topics:
                        yes, why = triaged.answers.get(topic.slug, (None, None))
                        await feedback_store.put_triage_result(
                            conn,
                            eval_run_id,
                            paper.id,
                            paper.version,
                            topic.slug,
                            yes,
                            why,
                            keywords.get(paper.id, {}).get(topic.slug),
                            triaged.error,
                            paper.revision,
                        )
            pairs = [(p, t) for p in papers if grade is None or p.id in grade for t in watched]
            log.info("eval %s: grading %s paper-topic pairs", eval_run_id, len(pairs))
            async for graded in watch.grade_many(pairs, run.gateway, config.limits.grading):
                run.add("graded" if graded.complete else "grading_failed")
                await feedback_store.put_grade_result(
                    conn,
                    eval_run_id,
                    graded.paper.id,
                    graded.topic.slug,
                    graded.judge,
                    graded.check,
                    graded.grade.value if graded.grade else None,
                    graded.split,
                    graded.error,
                )
        finally:  # a run that failed is finished too; its error is on the run record
            await feedback_store.finish_eval(conn, eval_run_id)
    return eval_run_id
