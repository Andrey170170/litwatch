"""What an adopted 0.x database still holds: its selections and its tiered verdicts.

Read by the topic import, the evaluation seeding and the data migration (`jobs.migrate`).
"""

from __future__ import annotations

import re
from typing import Any

import psycopg

from litwatch.core import Status, Topic, TopicKind

# 0.x treated every selection alike. v1 gives the research question a graded verdict per
# paper and everything else a weekly review; any selection not listed becomes Pulse.
KINDS = {
    "cot-faithfulness": TopicKind.WATCH,
    "cot-monitoring": TopicKind.WATCH,
    "interpretability": TopicKind.RADAR,
    "alignment-and-safety": TopicKind.RADAR,
}
_TIER_3 = re.compile(r"\b(is|are)\s+tier 3\.")


async def adopted(conn: psycopg.AsyncConnection) -> bool:
    row = await (await conn.execute("SELECT to_regclass('legacy_selections') AS t")).fetchone()
    return row["t"] is not None


async def topics(conn: psycopg.AsyncConnection) -> list[Topic]:
    """The active 0.x selections as v1 topics.

    A Watch topic keeps its subject, rubric and keywords as written. A field topic keeps
    only its subject: the 0.x tier 1 rubric and the audit keywords have no use there.
    """
    rows = await (
        await conn.execute("SELECT * FROM legacy_selections WHERE status = 'active' ORDER BY slug")
    ).fetchall()
    found = []
    for row in rows:
        kind = KINDS.get(row["slug"], TopicKind.PULSE)
        watch = kind == TopicKind.WATCH
        subject = (
            row["subject"] if watch else _TIER_3.sub(r"\1 not part of the field.", row["subject"])
        )
        found.append(
            Topic(
                slug=row["slug"],
                name=row["name"],
                kind=kind,
                subject=subject,
                rubric=row["rubric"] if watch else None,
                keywords=list(row["keywords"] or []) if watch else [],
                status=Status.ACTIVE,
            )
        )
    return found


async def verdicts(
    conn: psycopg.AsyncConnection, slugs: list[str], max_tier: int
) -> list[dict[str, Any]]:
    """0.x verdicts at `max_tier` or better for the given selections."""
    return await (
        await conn.execute(
            "SELECT paper_id, selection AS topic, tier, reason FROM legacy_verdicts "
            "WHERE selection = ANY(%s) AND tier <= %s ORDER BY paper_id, selection",
            (slugs, max_tier),
        )
    ).fetchall()


# -- the data migration (spec section 7) -----------------------------------------------------------

_GRADE = "(ARRAY['must_read', 'relevant', 'adjacent', 'none'])[v.tier]"


async def migrate_verdicts(conn: psycopg.AsyncConnection) -> dict[str, int]:
    """Keep each 0.x verdict on a Watch topic as a verdict marked `migrated`: its tier as the
    grade, and a single opinion carrying 0.x's reason and model. It is recorded when 0.x
    screened it and bound to the topic's current revision and the paper's current text. A
    pair that already has a migrated verdict is left alone. Returns the count per grade."""
    rows = await (
        await conn.execute(
            f"""
            INSERT INTO verdicts (paper_id, topic, topic_revision, paper_version, paper_revision,
                                  grade, judge, migrated, recorded_at)
            SELECT v.paper_id, v.selection, t.revision, p.version, p.revision, {_GRADE},
                   jsonb_build_object('grade', {_GRADE}, 'reason', v.reason, 'model', v.model),
                   true, v.screened_at
            FROM legacy_verdicts v
            JOIN papers p ON p.id = v.paper_id
            JOIN topics t ON t.slug = v.selection AND t.kind = 'watch'
            WHERE v.tier BETWEEN 1 AND 4
              AND NOT EXISTS (SELECT 1 FROM verdicts m WHERE m.migrated
                              AND m.paper_id = v.paper_id AND m.topic = v.selection)
            RETURNING grade
            """
        )
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["grade"]] = counts.get(row["grade"], 0) + 1
    return counts


async def migrate_memberships(conn: psycopg.AsyncConnection) -> int:
    """A 0.x verdict at tier 3 or better on a Radar or Pulse topic becomes a membership, dated
    when 0.x screened it (a review takes the members that joined in its period); the tier is
    dropped. A membership already there is kept."""
    cur = await conn.execute(
        """
        INSERT INTO memberships (paper_id, topic, topic_revision, paper_version, created_at)
        SELECT v.paper_id, v.selection, t.revision, p.version, v.screened_at
        FROM legacy_verdicts v
        JOIN papers p ON p.id = v.paper_id
        JOIN topics t ON t.slug = v.selection AND t.kind IN ('radar', 'pulse')
        WHERE v.tier BETWEEN 1 AND 3
        ON CONFLICT (paper_id, topic) DO NOTHING
        """
    )
    return cur.rowcount


async def migrate_regression(conn: psycopg.AsyncConnection) -> int:
    """0.x's tier 1 to 3 on the Watch topics join the regression set, as `eval seed --legacy`
    puts them there; model output kept to catch a regression, never the owner's word."""
    cur = await conn.execute(
        """
        INSERT INTO regression (paper_id, topic, tier, source)
        SELECT v.paper_id, v.selection, v.tier, 'legacy' FROM legacy_verdicts v
        JOIN topics t ON t.slug = v.selection AND t.kind = 'watch'
        WHERE v.tier BETWEEN 1 AND 3
        ON CONFLICT (paper_id, topic) DO NOTHING
        """
    )
    return cur.rowcount


async def unmatched(conn: psycopg.AsyncConnection) -> list[str]:
    """0.x selections with verdicts but no v1 topic of the same slug: left behind."""
    rows = await (
        await conn.execute(
            "SELECT DISTINCT selection FROM legacy_verdicts v "
            "WHERE NOT EXISTS (SELECT 1 FROM topics t WHERE t.slug = v.selection) ORDER BY 1"
        )
    ).fetchall()
    return [r["selection"] for r in rows]
