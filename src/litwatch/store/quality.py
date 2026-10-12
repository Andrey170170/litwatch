"""The queries behind Settings: Quality's lists, the correction rate, runs, and estimates."""

from __future__ import annotations

from typing import Any

import psycopg


async def _all(conn: psycopg.AsyncConnection, query: str, params: Any = ()) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def last_night(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
    return await (
        await conn.execute("SELECT * FROM runs WHERE kind = 'nightly' ORDER BY id DESC LIMIT 1")
    ).fetchone()


async def splits_since(conn: psycopg.AsyncConnection, since: Any) -> list[dict[str, Any]]:
    """Split verdicts recorded since `since` that are still current, with both opinions and
    the owner's label, for the owner to settle if they want to."""
    return await _all(
        conn,
        """
        SELECT * FROM (
            SELECT DISTINCT ON (v.paper_id, v.topic) v.paper_id, v.topic, v.grade, v.split,
                   v.judge, v.check_opinion AS check, v.recorded_at, p.title,
                   (SELECT row_to_json(l) FROM (SELECT value, grade FROM labels
                    WHERE paper_id = v.paper_id AND topic = v.topic) l) AS label
            FROM verdicts v JOIN papers p ON p.id = v.paper_id
            WHERE v.paper_id IN (SELECT paper_id FROM verdicts WHERE recorded_at >= %s)
            ORDER BY v.paper_id, v.topic, v.paper_revision DESC, v.topic_revision DESC,
                     v.recorded_at DESC, v.id DESC) current
        WHERE split AND recorded_at >= %s
        ORDER BY recorded_at DESC
        """,
        (since, since),
    )


async def ungraded(conn: psycopg.AsyncConnection, attempts: int) -> list[dict[str, Any]]:
    """Candidates grading gave up on, newest topic revision only."""
    return await _all(
        conn,
        """
        SELECT c.paper_id, c.topic, c.failed_runs, c.last_error, c.updated_at, p.title
        FROM candidates c JOIN papers p ON p.id = c.paper_id
        WHERE c.state = 'pending' AND c.failed_runs >= %s
          AND NOT EXISTS (SELECT 1 FROM candidates n WHERE n.paper_id = c.paper_id
                          AND n.topic = c.topic AND n.topic_revision > c.topic_revision)
        ORDER BY c.updated_at DESC
        """,
        (attempts,),
    )


async def awaiting_triage(conn: psycopg.AsyncConnection, attempts: int) -> list[dict[str, Any]]:
    return await _all(
        conn,
        "SELECT t.paper_id, t.failed_runs, t.last_error, p.title FROM triage t "
        "JOIN papers p ON p.id = t.paper_id WHERE t.state = 'failed' AND t.failed_runs >= %s "
        "ORDER BY t.failed_runs DESC, t.paper_id",
        (attempts,),
    )


async def correction_rate(conn: psycopg.AsyncConnection, days: int = 30) -> list[dict[str, Any]]:
    """Per Watch topic over the trailing days: papers shown in issues, and how many of them
    carry a thumb down meaning the paper should not have been there (not "already knew it")."""
    return await _all(
        conn,
        """
        SELECT t.slug AS topic, count(DISTINCT e.paper_id) AS shown,
               count(DISTINCT e.paper_id) FILTER (WHERE r.reason IN
                   ('not_for_me', 'unrelated', 'overrated')) AS corrected
        FROM topics t
        JOIN issue_entries e ON e.topic = t.slug
        JOIN issues i ON i.id = e.issue_id AND i.day >= current_date - %s
        LEFT JOIN reactions r ON r.paper_id = e.paper_id AND r.topic = t.slug
                             AND r.value = 'down'
        WHERE t.kind = 'watch'
        GROUP BY t.slug, t.position ORDER BY t.position
        """,
        (days,),
    )


async def collected_reactions(conn: psycopg.AsyncConnection, limit: int = 200) -> list[dict]:
    """Reactions that move nothing by themselves (decision 17), for the owner or an agent."""
    return await _all(
        conn,
        """
        SELECT r.paper_id, r.topic, r.value, r.reason, r.reacted_at, p.title
        FROM reactions r JOIN papers p ON p.id = r.paper_id
        LEFT JOIN topics t ON t.slug = r.topic
        WHERE r.topic IS NULL OR t.kind <> 'watch'
           OR r.reason IS NULL OR r.reason NOT IN ('unrelated', 'great_find')
        ORDER BY r.reacted_at DESC LIMIT %s
        """,
        (limit,),
    )


async def runs(conn: psycopg.AsyncConnection, limit: int = 30) -> list[dict[str, Any]]:
    return await _all(
        conn,
        "SELECT id, kind, started_at, finished_at, cost, error, counts, fetched, inserted, "
        "updated FROM runs ORDER BY id DESC LIMIT %s",
        (limit,),
    )


async def cost_per_call(conn: psycopg.AsyncConnection, days: int = 30) -> dict[str, float]:
    """The average charged cost of one call per role over the trailing days."""
    rows = await _all(
        conn,
        """
        SELECT operation AS role, avg((usage->>'cost')::numeric) AS cost
        FROM inference_calls
        WHERE started_at >= now() - make_interval(days => %s)
          AND usage->>'cost' IS NOT NULL AND NOT coalesce((usage->>'reused')::bool, false)
        GROUP BY operation
        """,
        (days,),
    )
    return {r["role"]: float(r["cost"]) for r in rows}


async def keyword_counts(conn: psycopg.AsyncConnection, patterns: list[str]) -> dict[str, int]:
    """How many stored papers each keyword pattern matches in title or abstract. Postgres
    regular expressions, case-insensitive, which agree with Python's on ordinary patterns."""
    out = {}
    for pattern in patterns:
        try:
            row = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM papers WHERE title ~* %s OR abstract ~* %s",
                    (pattern, pattern),
                )
            ).fetchone()
            out[pattern] = row["n"]
        except psycopg.errors.InvalidRegularExpression:
            out[pattern] = -1
    return out


async def keyword_papers(conn: psycopg.AsyncConnection, patterns: list[str]) -> list[str]:
    if not patterns:
        return []
    alternation = "|".join(f"({p})" for p in patterns)
    rows = await _all(
        conn,
        "SELECT id FROM papers WHERE title ~* %s OR abstract ~* %s ORDER BY id",
        (alternation, alternation),
    )
    return [r["id"] for r in rows]


async def papers_between(conn: psycopg.AsyncConnection, since: Any, until: Any) -> list[str]:
    rows = await _all(
        conn,
        "SELECT id FROM papers WHERE published >= %s AND published < %s ORDER BY id",
        (since, until),
    )
    return [r["id"] for r in rows]


async def candidate_papers(conn: psycopg.AsyncConnection, topic: str) -> list[str]:
    rows = await _all(
        conn, "SELECT DISTINCT paper_id FROM candidates WHERE topic = %s ORDER BY 1", (topic,)
    )
    return [r["paper_id"] for r in rows]


async def evidence_papers(conn: psycopg.AsyncConnection, topic: str) -> list[str]:
    """The topic's labelled papers (unsure aside) and reported misses."""
    rows = await _all(
        conn,
        "SELECT paper_id FROM labels WHERE topic = %(t)s AND value <> 'unsure' "
        "UNION SELECT paper_id FROM misses WHERE topic = %(t)s ORDER BY 1",
        {"t": topic},
    )
    return [r["paper_id"] for r in rows]
