"""The queries behind Settings: Quality's lists and the runs."""

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
    the owner's label, for the owner to settle if they want to: followed Focus topics only,
    the ones a label can be given for."""
    return await _all(
        conn,
        """
        SELECT * FROM (
            SELECT DISTINCT ON (v.paper_id, v.topic) v.paper_id, v.topic, v.grade, v.split,
                   v.judge, v.check_opinion AS check, v.recorded_at, p.title,
                   (SELECT row_to_json(l) FROM (SELECT value, grade FROM labels
                    WHERE paper_id = v.paper_id AND topic = v.topic) l) AS label
            FROM verdicts v JOIN papers p ON p.id = v.paper_id
            JOIN topics t ON t.class = v.topic AND t.depth = 'focus'
            WHERE v.paper_id IN (SELECT paper_id FROM verdicts WHERE recorded_at >= %s)
            ORDER BY v.paper_id, v.topic, v.paper_revision DESC, v.topic_revision DESC,
                     v.recorded_at DESC, v.id DESC) current
        WHERE split AND recorded_at >= %s
        ORDER BY recorded_at DESC
        """,
        (since, since),
    )


async def collected_reactions(conn: psycopg.AsyncConnection, limit: int = 200) -> list[dict]:
    """Reactions that move nothing by themselves (decision 17), for the owner or an agent."""
    return await _all(
        conn,
        """
        SELECT r.paper_id, r.topic, r.value, r.reason, r.reacted_at, p.title
        FROM reactions r JOIN papers p ON p.id = r.paper_id
        LEFT JOIN topics t ON t.class = r.topic
        WHERE t.depth IS DISTINCT FROM 'focus'
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
