"""The queries behind Settings: Quality's lists and the runs."""

from __future__ import annotations

from typing import Any

import psycopg

from litwatch.core.evaluation import recall
from litwatch.store import evaluation


async def _all(conn: psycopg.AsyncConnection, query: str, params: Any = ()) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def last_night(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
    return await (
        await conn.execute("SELECT * FROM runs WHERE kind = 'nightly' ORDER BY id DESC LIMIT 1")
    ).fetchone()


async def splits_since(conn: psycopg.AsyncConnection, since: Any) -> list[dict[str, Any]]:
    """Split or escalated verdicts recorded since `since` that are still current (Focus
    splits, Area escalations), with every grader's answer and the owner's label, for the owner
    to settle if they want to: followed topics only."""
    return await _all(
        conn,
        """
        SELECT * FROM (
            SELECT DISTINCT ON (v.paper_id, v.topic) v.paper_id, v.topic, t.depth, v.fit,
                   v.reason, v.split, v.escalated, v.opinions, v.recorded_at, p.title,
                   (SELECT row_to_json(l) FROM (SELECT value FROM labels
                    WHERE paper_id = v.paper_id AND topic = v.topic) l) AS label
            FROM verdicts v JOIN papers p ON p.id = v.paper_id
            JOIN topics t ON t.class = v.topic
            WHERE v.paper_id IN (SELECT paper_id FROM verdicts WHERE recorded_at >= %s)
            ORDER BY v.paper_id, v.topic, v.paper_revision DESC, v.recorded_at DESC,
                     v.id DESC) current
        WHERE (split OR escalated) AND recorded_at >= %s
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
           OR r.reason IS NULL OR r.reason NOT IN ('wrong_topic', 'great_find')
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


async def replay_recall(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
    """Recall per Focus topic from the latest replay (feedback.md, evaluation): the four numbers
    for each cohort and topic with their n, never pooled, and the run they come from; None
    before any replay. A replay still running, or cut short, counts what has concluded."""
    eval_run_id = await evaluation.latest(conn, "replay")
    if eval_run_id is None:
        return None
    run = await evaluation.get(conn, eval_run_id)
    return {
        "eval_run": eval_run_id,
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "cost": float(run["cost"]) if run["cost"] is not None else None,
        "recall": recall(await evaluation.outcomes(conn, eval_run_id)),
    }
