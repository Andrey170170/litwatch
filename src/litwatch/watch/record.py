"""Store a grading as a verdict, or as a failed attempt on its candidate."""

from __future__ import annotations

import psycopg

from litwatch import events
from litwatch.core import Verdict
from litwatch.store import verdicts
from litwatch.watch.grade import Graded


async def record(
    conn: psycopg.AsyncConnection, graded: Graded, candidate_revision: int | None = None
) -> Verdict | None:
    """Record the verdict and mark the candidate graded.

    Without two valid opinions no verdict is made: the candidate stays pending with the
    failure counted, and the previous verdict, if any, stays current. `candidate_revision`
    names the candidate row when it was made under an earlier topic revision than the one
    graded under; the verdict always records the revision it was graded under.
    """
    paper, topic = graded.paper, graded.topic
    made_under = candidate_revision or topic.revision
    async with conn.transaction():
        if not graded.complete:
            await verdicts.mark_failed(conn, paper.id, topic.slug, made_under, graded.error)
            return None
        verdict = await verdicts.add(
            conn,
            Verdict(
                paper_id=paper.id,
                topic=topic.slug,
                topic_revision=topic.revision,
                paper_version=paper.version,
                paper_revision=paper.revision,
                grade=graded.grade,
                split=graded.split,
                judge=graded.judge,
                check=graded.check,
            ),
        )
        await verdicts.mark_graded(conn, paper.id, topic.slug, made_under)
        await events.emit(
            conn,
            events.VERDICT_RECORDED,
            paper.id,
            topic=topic.slug,
            grade=verdict.grade.value,
            split=verdict.split,
            verdict_id=verdict.id,
        )
    return verdict
