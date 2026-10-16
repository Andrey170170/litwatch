"""The queries behind Settings: Quality's lists, the misses and their traces, the runs, and
the counts the Topics and Taxonomy pages show."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any

import psycopg

from litwatch.core import Taxonomy
from litwatch.core.classification import keyword_hit, with_ancestors
from litwatch.core.evaluation import recall
from litwatch.store import evaluation
from litwatch.store.reading import SHOWN_MEMBER


async def _all(conn: psycopg.AsyncConnection, query: str, params: Any = ()) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def last_night(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
    return await (
        await conn.execute("SELECT * FROM runs WHERE kind = 'nightly' ORDER BY id DESC LIMIT 1")
    ).fetchone()


def splits_window(night: dict[str, Any] | None) -> datetime | date:
    """Since when Needs you lists splits, on every surface that counts or shows them: last
    night's run, or the last day while no night has run yet."""
    return night["started_at"] if night else date.today() - timedelta(days=1)


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


async def reactions_by_topic(conn: psycopg.AsyncConnection, days: int = 30) -> list[dict]:
    """Thumbs per followed topic and reason over the trailing days (feedback.md, reactions:
    each reason points at a stage), for Overview's table, in the owner's topic order. A thumb
    without a reason has `reason` null."""
    return await _all(
        conn,
        """
        SELECT r.topic, r.reason, count(*) AS n FROM reactions r
        JOIN topics t ON t.class = r.topic
        WHERE r.reacted_at >= now() - make_interval(days => %s)
        GROUP BY t.position, r.topic, r.reason ORDER BY t.position, r.topic, r.reason
        """,
        (days,),
    )


async def count_misses(conn: psycopg.AsyncConnection) -> int:
    row = await (await conn.execute("SELECT count(*) AS n FROM misses")).fetchone()
    return row["n"]


async def misses(conn: psycopg.AsyncConnection, tree: Taxonomy | None) -> list[dict[str, Any]]:
    """Reported misses, newest first, each with what the stages concluded for the owner's topic
    on the paper's current text (feedback.md, misses): whether it has been classified and the
    classifier's probability for the topic's class, which of the classifier, the uncertain band
    and the keyword check made it a member (a classifier membership anywhere under the class
    counts, as it stands in the class, and reads `below` when it is under the class only; the
    band and the keyword check admit a paper to one Focus class only), and the graders'
    opinions from the current verdict. Null where a stage has not run on this text."""
    # Each followed topic's class with every class under it, as (topic, class) pairs.
    pairs = [(t.slug, c) for t in (tree.topics if tree else []) for c in tree.subtree(t.slug)]
    return await _all(
        conn,
        """
        WITH under AS (SELECT * FROM unnest(%s::text[], %s::text[]) AS under (topic, class))
        SELECT m.id, m.paper_id, m.topic, m.reported_at, p.title,
               -- the classifier's own: a classification by hand is the owner's, not a stage
               EXISTS (SELECT 1 FROM classifications c WHERE c.paper_id = m.paper_id
                       AND c.paper_revision = p.revision AND c.model <> 'owner') AS classified,
               (SELECT (c.response->'probabilities'->>m.topic)::float FROM classifications c
                WHERE c.paper_id = m.paper_id AND c.paper_revision = p.revision
                  AND c.model <> 'owner'
                ORDER BY c.id DESC LIMIT 1) AS probability,
               -- 'below': a classifier membership in a class under the topic's, not in it
               (SELECT coalesce(array_agg(DISTINCT CASE WHEN x.class = m.topic THEN x.source
                                                        ELSE 'below' END), '{}')
                FROM memberships x
                WHERE x.paper_id = m.paper_id AND x.member AND x.paper_revision = p.revision
                  AND x.source IN ('classifier', 'uncertain', 'keyword')
                  AND (x.class = m.topic OR (x.source = 'classifier' AND x.class IN
                       (SELECT class FROM under WHERE under.topic = m.topic)))) AS sources,
               (SELECT v.opinions FROM verdicts v WHERE v.paper_id = m.paper_id
                AND v.topic = m.topic AND v.paper_revision = p.revision
                ORDER BY v.recorded_at DESC, v.id DESC LIMIT 1) AS opinions
        FROM misses m JOIN papers p ON p.id = m.paper_id
        ORDER BY m.reported_at DESC, m.id DESC
        """,
        ([t for t, _ in pairs], [c for _, c in pairs]),
    )


async def class_members(conn: psycopg.AsyncConnection, tree: Taxonomy) -> dict[str, int]:
    """Papers standing in each class or any class under it (the memberships a page shows), by
    class slug; a class with none is left out."""
    pairs = [(c.slug, a) for c in tree.classes for a in with_ancestors(tree, {c.slug})]
    rows = await _all(
        conn,
        f"""
        SELECT map.target, count(DISTINCT m.paper_id) AS n
        FROM memberships m
        JOIN unnest(%s::text[], %s::text[]) AS map (class, target) ON map.class = m.class
        JOIN papers p ON p.id = m.paper_id AND p.dismissed_at IS NULL
        WHERE {SHOWN_MEMBER}
        GROUP BY map.target
        """,
        ([c for c, _ in pairs], [a for _, a in pairs]),
    )
    return {r["target"]: r["n"] for r in rows}


async def topic_revisions(conn: psycopg.AsyncConnection) -> dict[str, int]:
    """The current revision of every followed topic, by class slug."""
    rows = await _all(conn, "SELECT class, revision FROM topics")
    return {r["class"]: r["revision"] for r in rows}


async def keyword_matches(
    conn: psycopg.AsyncConnection, patterns: list[str], days: int = 90
) -> dict[str, int]:
    """How many papers published in the trailing days each keyword pattern matches, as the
    keyword check matches them (core.classification.keyword_hit)."""
    if not patterns:
        return {}
    rows = await _all(
        conn,
        "SELECT title, abstract FROM papers WHERE dismissed_at IS NULL "
        "AND published >= now() - make_interval(days => %s)",
        (days,),
    )

    # The regexes run over the whole window; a thread keeps the event loop free meanwhile.
    def count() -> dict[str, int]:
        return {k: sum(keyword_hit(k, r["title"], r["abstract"]) for r in rows) for k in patterns}

    return await asyncio.to_thread(count)
