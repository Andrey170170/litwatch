"""What an adopted 0.x database still holds: its selections and its tiered verdicts.

Read by the topic import and the evaluation seeding now, and by the data migration later.
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
