"""Verdicts: every grading of a paper for a topic, each its own row (data.md, `verdicts`)."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Opinion, Verdict
from litwatch.core.classification import Membership

# A current verdict that waits for the owner's word, over its columns (pipeline.md, section 4):
# a Focus split, an Area escalation whose two graders disagreed (also a split), or a central or
# related grade from one lab. An escalation where `check` agreed with `judge` asks nothing.
ASKS_WORD = "(split OR (one_lab AND fit IN ('central', 'related')))"


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
        one_lab=row["one_lab"],
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
                                  opinions, split, escalated, one_lab)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
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
                verdict.one_lab,
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


async def candidates(
    conn: psycopg.AsyncConnection,
    runs: int,
    limit: int | None = None,
    ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The papers with grading work, at most `limit` and among `ids` when given, those without a
    grading pass on their current text first, then oldest first. A paper not dismissed and
    classified at its current revision has work when it has no grading pass yet on that text, a
    grading on an active topic that failed fewer than `runs` runs, or a miss reported for an active
    topic with no verdict on that text and no grading stopped there. Each comes with its direct
    membership rows (`memberships`), whether its grading pass is done (`passed`), the topics graded
    on its current text (`graded`), the topics whose current verdict on an earlier revision has a
    Fit above none (`held`; pipeline.md, section 8), the failed grading runs in a row per topic on
    its current text (`failed`), the roles whose every model refused per topic (`refused`) and the
    active topics a miss was reported for (`missed`)."""
    rows = await (
        await conn.execute(
            """
            WITH chosen AS (
                SELECT * FROM (
                    SELECT p.id, p.revision, p.fetched_at,
                           EXISTS (SELECT 1 FROM grading_passes g WHERE g.paper_id = p.id
                                   AND g.paper_revision = p.revision) AS passed
                    FROM papers p
                    WHERE p.dismissed_at IS NULL
                      AND (%(ids)s::text[] IS NULL OR p.id = ANY(%(ids)s))
                      AND EXISTS (SELECT 1 FROM classifications c
                                  WHERE c.paper_id = p.id AND c.paper_revision = p.revision)
                ) p
                WHERE NOT p.passed
                   OR EXISTS (SELECT 1 FROM step_failures f
                              JOIN topics t ON t.class = f.topic AND t.status = 'active'
                              WHERE f.paper_id = p.id AND f.step = 'grading'
                                AND f.paper_revision = p.revision AND f.failed_runs < %(runs)s)
                   OR EXISTS (SELECT 1 FROM misses x
                              JOIN topics t ON t.class = x.topic AND t.status = 'active'
                              WHERE x.paper_id = p.id
                                AND NOT EXISTS (SELECT 1 FROM verdicts v
                                                WHERE v.paper_id = p.id AND v.topic = x.topic
                                                  AND v.paper_revision = p.revision)
                                AND NOT EXISTS (SELECT 1 FROM step_failures f
                                                WHERE f.paper_id = p.id AND f.step = 'grading'
                                                  AND f.topic = x.topic
                                                  AND f.paper_revision = p.revision
                                                  AND f.failed_runs >= %(runs)s))
                ORDER BY p.passed, p.fetched_at, p.id
                LIMIT %(limit)s
            )
            SELECT p.id AS paper_id, p.revision, p.passed,
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
                    AND f.paper_revision = p.revision) AS failed,
                   (SELECT coalesce(jsonb_object_agg(f.topic, f.refused), '{}')
                    FROM step_failures f WHERE f.paper_id = p.id AND f.step = 'grading'
                    AND f.paper_revision = p.revision AND f.refused <> '{}') AS refused,
                   (SELECT coalesce(array_agg(DISTINCT x.topic), '{}') FROM misses x
                    JOIN topics t ON t.class = x.topic AND t.status = 'active'
                    WHERE x.paper_id = p.id) AS missed
            FROM chosen p
            ORDER BY p.passed, p.fetched_at, p.id
            """,
            {"runs": runs, "limit": limit, "ids": ids},
        )
    ).fetchall()
    for r in rows:
        r["memberships"] = [Membership.model_validate(m) for m in r["memberships"]]
    return rows


async def passed(conn: psycopg.AsyncConnection, passes: list[tuple[str, int]]) -> None:
    """Record the grading pass of each (paper, revision)."""
    for paper_id, revision in passes:
        await conn.execute(
            "INSERT INTO grading_passes (paper_id, paper_revision) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (paper_id, revision),
        )


async def ungraded_under(
    conn: psycopg.AsyncConnection, topic: str, version: str, ids: list[str]
) -> list[str]:
    """Those of `ids`, in their order, not dismissed and with no verdict for the topic under its
    `version` on their current text: what a re-run of the topic still has to grade."""
    rows = await (
        await conn.execute(
            """
            SELECT p.id FROM unnest(%(ids)s::text[]) WITH ORDINALITY AS x (id, n)
            JOIN papers p ON p.id = x.id AND p.dismissed_at IS NULL
            WHERE NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.paper_id = p.id
                              AND v.topic = %(topic)s AND v.topic_version = %(version)s
                              AND v.paper_revision = p.revision)
            ORDER BY x.n
            """,
            {"ids": ids, "topic": topic, "version": version},
        )
    ).fetchall()
    return [r["id"] for r in rows]


async def escalated_share(conn: psycopg.AsyncConnection, topic: str, last: int = 200) -> float:
    """The share of the topic's latest `last` Area verdicts that escalated to `check`: what an
    estimate expects of an Area re-run. 1 with none yet."""
    row = await (
        await conn.execute(
            "SELECT avg(escalated::int) AS share FROM (SELECT escalated FROM verdicts "
            "WHERE topic = %s AND opinions ? 'area_check' ORDER BY id DESC LIMIT %s) recent",
            (topic, last),
        )
    ).fetchone()
    return 1.0 if row["share"] is None else float(row["share"])
