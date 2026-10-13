"""Reviews of Radar and Pulse topics and recaps of Watch topics, with their picks."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import psycopg

from litwatch.core import Paper
from litwatch.store.papers import COLUMNS, row_to_paper

# A review's picks as a JSON list, in order, with what the reading side shows of each paper,
# including the owner's thumb on it for the review's topic (spec decision 17).
PICKS = """coalesce((SELECT jsonb_agg(jsonb_build_object(
                'paper_id', k.paper_id, 'why', k.why, 'title', p.title,
                'authors', CASE WHEN jsonb_typeof(p.authors) = 'array' THEN p.authors ELSE '[]' END,
                'published', p.published, 'saved', coalesce(l.saved, false),
                'reaction', x.value, 'reaction_reason', x.reason) ORDER BY k.position)
            FROM review_picks k JOIN papers p ON p.id = k.paper_id
            LEFT JOIN library l ON l.paper_id = k.paper_id
            LEFT JOIN reactions x ON x.paper_id = k.paper_id AND x.topic = r.topic
            WHERE k.review_id = r.id), '[]') AS picks"""


async def members(
    conn: psycopg.AsyncConnection, topic: str, since: datetime, until: datetime
) -> list[Paper]:
    """Papers that joined the topic's field in the window, oldest first."""
    rows = await (
        await conn.execute(
            f"SELECT {COLUMNS} FROM memberships m JOIN papers p ON p.id = m.paper_id "
            "WHERE m.topic = %s AND m.created_at >= %s AND m.created_at < %s "
            "ORDER BY m.created_at, p.id",
            (topic, since, until),
        )
    ).fetchall()
    return [row_to_paper(r) for r in rows]


async def put(
    conn: psycopg.AsyncConnection,
    topic: str,
    period: str,
    day: date,
    *,
    kind: str,
    state: str,
    topic_revision: int | None = None,
    text: str | None = None,
    error: str | None = None,
    members: int = 0,
    picks: list[tuple[str, str]] | None = None,
) -> int:
    """Store the period's review, replacing an earlier attempt and its picks."""
    async with conn.transaction():
        row = await (
            await conn.execute(
                """
                INSERT INTO reviews (topic, kind, period, day, topic_revision, text, state, error,
                                     members)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (topic, period, day) DO UPDATE SET kind = EXCLUDED.kind,
                    topic_revision = EXCLUDED.topic_revision, text = EXCLUDED.text,
                    state = EXCLUDED.state, error = EXCLUDED.error, members = EXCLUDED.members,
                    written_at = clock_timestamp()
                RETURNING id
                """,
                (topic, kind, period, day, topic_revision, text, state, error, members),
            )
        ).fetchone()
        await conn.execute("DELETE FROM review_picks WHERE review_id = %s", (row["id"],))
        for position, (paper_id, why) in enumerate(picks or [], 1):
            await conn.execute(
                "INSERT INTO review_picks (review_id, paper_id, position, why) "
                "VALUES (%s, %s, %s, %s)",
                (row["id"], paper_id, position, why),
            )
    return row["id"]


async def get(
    conn: psycopg.AsyncConnection, topic: str, period: str, day: date
) -> dict[str, Any] | None:
    return await (
        await conn.execute(
            "SELECT * FROM reviews WHERE topic = %s AND period = %s AND day = %s",
            (topic, period, day),
        )
    ).fetchone()


async def for_period(conn: psycopg.AsyncConnection, period: str, day: date) -> list[dict]:
    """The period's reviews and recaps with their picks, in the owner's topic order."""
    return await (
        await conn.execute(
            f"""
            SELECT r.*, t.name AS topic_name,
                   {PICKS}
            FROM reviews r JOIN topics t ON t.slug = r.topic
            WHERE r.period = %s AND r.day = %s
            ORDER BY array_position(ARRAY['watch', 'radar', 'pulse'], r.kind), t.position
            """,
            (period, day),
        )
    ).fetchall()


async def for_topic(conn: psycopg.AsyncConnection, topic: str, limit: int = 20) -> list[dict]:
    """A topic's field reviews, newest first, with their picks: not the recaps it got while it
    was a Watch topic."""
    return await (
        await conn.execute(
            f"""
            SELECT r.*,
                   {PICKS}
            FROM reviews r WHERE r.topic = %s AND r.kind <> 'watch'
            -- Newest by the end of the span reviewed, not by when it was (re)written: a week
            -- ends after its days, and a retried old week stays where its week is.
            ORDER BY r.day + CASE r.period WHEN 'week' THEN 7 ELSE 1 END DESC,
                     r.period = 'week' DESC, r.day DESC LIMIT %s
            """,
            (topic, limit),
        )
    ).fetchall()


async def failed_days(
    conn: psycopg.AsyncConnection, period: str, since: date, until: date
) -> list[date]:
    """The days (or weeks, by their Monday) in [since, until) with a review that failed."""
    rows = await (
        await conn.execute(
            "SELECT DISTINCT day FROM reviews WHERE period = %s AND state = 'failed' "
            "AND day >= %s AND day < %s ORDER BY day",
            (period, since, until),
        )
    ).fetchall()
    return [r["day"] for r in rows]


async def week_entries(
    conn: psycopg.AsyncConnection, topic: str, monday: date
) -> list[dict[str, Any]]:
    """The topic's entries in the week's daily issues, with their takes."""
    return await (
        await conn.execute(
            """
            SELECT e.paper_id, e.grade, p.title, k.text AS take, i.day,
                   x.value AS reaction, x.reason AS reaction_reason
            FROM issue_entries e JOIN issues i ON i.id = e.issue_id AND i.kind = 'daily'
            JOIN papers p ON p.id = e.paper_id LEFT JOIN takes k ON k.id = e.take_id
            LEFT JOIN reactions x ON x.paper_id = e.paper_id AND x.topic = e.topic
            WHERE e.topic = %s AND i.day >= %s AND i.day < %s::date + 7
            ORDER BY i.day, e.position
            """,
            (topic, monday, monday),
        )
    ).fetchall()


async def week_set_aside(
    conn: psycopg.AsyncConnection, topic: str, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Papers once announced for the topic that the owner put below relevant in the window,
    as it happened, whatever the paper's grade is now."""
    return await (
        await conn.execute(
            """
            SELECT DISTINCT p.id AS paper_id, p.title,
                   x.value AS reaction, x.reason AS reaction_reason
            FROM set_asides a JOIN papers p ON p.id = a.paper_id
            LEFT JOIN reactions x ON x.paper_id = a.paper_id AND x.topic = a.topic
            WHERE a.topic = %s AND a.at >= %s AND a.at < %s
            """,
            (topic, since, until),
        )
    ).fetchall()


async def week_misses(
    conn: psycopg.AsyncConnection, topic: str, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    return await (
        await conn.execute(
            "SELECT m.paper_id, p.title, x.value AS reaction, x.reason AS reaction_reason "
            "FROM misses m JOIN papers p ON p.id = m.paper_id "
            "LEFT JOIN reactions x ON x.paper_id = m.paper_id AND x.topic = m.topic "
            "WHERE m.topic = %s AND m.reported_at >= %s AND m.reported_at < %s "
            "ORDER BY m.reported_at",
            (topic, since, until),
        )
    ).fetchall()
