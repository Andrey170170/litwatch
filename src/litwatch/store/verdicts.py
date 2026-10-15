"""Verdicts: every grading of a paper for a topic, each its own row (data.md, `verdicts`)."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Opinion, Verdict
from litwatch.core.classification import Membership


def _verdict(row: dict[str, Any]) -> Verdict:
    return Verdict(
        id=row["id"],
        paper_id=row["paper_id"],
        topic=row["topic"],
        topic_version=row["topic_version"],
        paper_revision=row["paper_revision"],
        fit=row["fit"],
        reason=row["reason"],
        opinions={k: Opinion.model_validate(v) for k, v in row["opinions"].items()},
        split=row["split"],
        escalated=row["escalated"],
        recorded_at=row["recorded_at"],
    )


async def add(conn: psycopg.AsyncConnection, verdict: Verdict) -> Verdict:
    """Record a grading as a new row; earlier rows stay as history."""
    opinions = {
        k: o.model_dump(mode="json", exclude_none=True) for k, o in verdict.opinions.items()
    }
    row = await (
        await conn.execute(
            """
            INSERT INTO verdicts (paper_id, topic, topic_version, paper_revision, fit, reason,
                                  opinions, split, escalated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (
                verdict.paper_id,
                verdict.topic,
                verdict.topic_version,
                verdict.paper_revision,
                verdict.fit.value,
                verdict.reason,
                Jsonb(opinions),
                verdict.split,
                verdict.escalated,
            ),
        )
    ).fetchone()
    return _verdict(row)


async def for_paper(conn: psycopg.AsyncConnection, paper_id: str) -> list[Verdict]:
    """Every verdict on the paper, for every topic."""
    rows = await (
        await conn.execute("SELECT * FROM verdicts WHERE paper_id = %s ORDER BY id", (paper_id,))
    ).fetchall()
    return [_verdict(r) for r in rows]


async def candidates(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    """What decides which gradings are due, for every paper whose current text is classified
    and that is not dismissed, oldest first: its direct membership rows (`memberships`), the
    topics already graded on its current text (`graded`), the topics whose current verdict on
    an earlier revision has a Fit above none (`held`; pipeline.md, section 8), and the failed
    grading runs in a row per topic on its current text (`failed`)."""
    rows = await (
        await conn.execute(
            """
            SELECT p.id AS paper_id,
                   (SELECT coalesce(jsonb_agg(jsonb_build_object(
                        'slug', m.class, 'source', m.source, 'member', m.member)), '[]')
                    FROM memberships m WHERE m.paper_id = p.id) AS memberships,
                   (SELECT coalesce(array_agg(DISTINCT v.topic), '{}') FROM verdicts v
                    WHERE v.paper_id = p.id AND v.paper_revision = p.revision) AS graded,
                   (SELECT coalesce(array_agg(o.topic), '{}') FROM (
                        SELECT DISTINCT ON (v.topic) v.topic, v.fit FROM verdicts v
                        WHERE v.paper_id = p.id AND v.paper_revision < p.revision
                        ORDER BY v.topic, v.paper_revision DESC, v.recorded_at DESC, v.id DESC
                    ) o WHERE o.fit <> 'none') AS held,
                   (SELECT coalesce(jsonb_object_agg(f.topic, f.failed_runs), '{}')
                    FROM step_failures f WHERE f.paper_id = p.id AND f.step = 'grading'
                    AND f.paper_revision = p.revision) AS failed
            FROM papers p
            WHERE p.dismissed_at IS NULL
              AND EXISTS (SELECT 1 FROM classifications c
                          WHERE c.paper_id = p.id AND c.paper_revision = p.revision)
            ORDER BY p.fetched_at, p.id
            """
        )
    ).fetchall()
    for r in rows:
        r["memberships"] = [Membership.model_validate(m) for m in r["memberships"]]
    return rows
