"""Evaluation runs: what a replay was asked, the labels it was measured against, what each stage
concluded per paper and topic, and what it cost. Nothing here touches the pipeline's tables."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Paper
from litwatch.core.evaluation import Outcome
from litwatch.core.grading import Opinion


async def start(
    conn: psycopg.AsyncConnection,
    kind: str,
    models: dict[str, Any],
    topics: dict[str, str],
    params: dict[str, Any],
    note: str | None = None,
) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO eval_runs (kind, models, topics, params, note) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (kind, Jsonb(models), Jsonb(topics), Jsonb(params), note),
        )
    ).fetchone()
    return row["id"]


async def get(conn: psycopg.AsyncConnection, eval_run_id: int) -> dict[str, Any] | None:
    """The run with its cost: the sum over every run record it ran under (None before any)."""
    return await (
        await conn.execute(
            "SELECT e.*, (SELECT sum(r.cost) FROM runs r WHERE r.id = ANY(e.run_ids)) AS cost "
            "FROM eval_runs e WHERE e.id = %s",
            (eval_run_id,),
        )
    ).fetchone()


async def latest(conn: psycopg.AsyncConnection, kind: str) -> int | None:
    row = await (
        await conn.execute(
            "SELECT id FROM eval_runs WHERE kind = %s ORDER BY id DESC LIMIT 1", (kind,)
        )
    ).fetchone()
    return row["id"] if row else None


async def ran_under(conn: psycopg.AsyncConnection, eval_run_id: int, run_id: int) -> None:
    """Add a run record whose cost the evaluation carries."""
    await conn.execute(
        "UPDATE eval_runs SET run_ids = array_append(run_ids, %s), "
        "run_id = coalesce(run_id, %s), finished_at = NULL WHERE id = %s",
        (run_id, run_id, eval_run_id),
    )


async def finish(conn: psycopg.AsyncConnection, eval_run_id: int) -> None:
    await conn.execute("UPDATE eval_runs SET finished_at = now() WHERE id = %s", (eval_run_id,))


async def put_labels(
    conn: psycopg.AsyncConnection, eval_run_id: int, labels: list[tuple[str, str, str, bool]]
) -> None:
    """(paper, topic, cohort, positive) for each label the run is measured against."""
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO eval_labels (eval_run_id, paper_id, topic, cohort, positive) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            [(eval_run_id, *label) for label in labels],
        )


async def pairs(conn: psycopg.AsyncConnection, eval_run_id: int) -> list[dict[str, Any]]:
    """Each paper and topic the run covers, with what it has concluded so far."""
    return await (
        await conn.execute(
            """
            SELECT DISTINCT l.paper_id, l.topic, r.paper_revision, r.classifier, r.fit
            FROM eval_labels l LEFT JOIN eval_results r
              ON r.eval_run_id = l.eval_run_id AND r.paper_id = l.paper_id AND r.topic = l.topic
            WHERE l.eval_run_id = %s ORDER BY l.paper_id, l.topic
            """,
            (eval_run_id,),
        )
    ).fetchall()


async def forget(conn: psycopg.AsyncConnection, eval_run_id: int, paper_ids: list[str]) -> None:
    """Drop what the run concluded on these papers, so both stages run on them again."""
    await conn.execute(
        "DELETE FROM eval_results WHERE eval_run_id = %s AND paper_id = ANY(%s)",
        (eval_run_id, paper_ids),
    )


async def put_classified(
    conn: psycopg.AsyncConnection,
    eval_run_id: int,
    paper: Paper,
    topic: str,
    *,
    classifier: bool | None = None,
    uncertain: bool | None = None,
    probability: float | None = None,
    keyword: str | None = None,
    error: str | None = None,
) -> None:
    """What classification concluded for one paper and topic, replacing an earlier attempt."""
    await conn.execute(
        """
        INSERT INTO eval_results (eval_run_id, paper_id, topic, paper_revision, paper_version,
                                  classifier, uncertain, probability, keyword, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (eval_run_id, paper_id, topic) DO UPDATE SET
            paper_revision = EXCLUDED.paper_revision, paper_version = EXCLUDED.paper_version,
            classifier = EXCLUDED.classifier, uncertain = EXCLUDED.uncertain,
            probability = EXCLUDED.probability, keyword = EXCLUDED.keyword,
            error = EXCLUDED.error
        """,
        (
            eval_run_id,
            paper.id,
            topic,
            paper.revision,
            paper.version,
            classifier,
            uncertain,
            probability,
            keyword,
            error,
        ),
    )


async def put_graded(
    conn: psycopg.AsyncConnection,
    eval_run_id: int,
    paper_id: str,
    paper_revision: int,
    topic: str,
    *,
    fit: str | None,
    opinions: dict[str, Opinion],
    split: bool | None,
    error: str | None,
) -> None:
    """What grading concluded for one paper and topic, replacing an earlier attempt."""

    def opinion(role: str) -> Jsonb | None:
        o = opinions.get(role)
        return Jsonb(o.model_dump(mode="json", exclude_none=True)) if o else None

    await conn.execute(
        """
        INSERT INTO eval_results (eval_run_id, paper_id, topic, paper_revision, fit, judge,
                                  check_opinion, split, grade_error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (eval_run_id, paper_id, topic) DO UPDATE SET
            fit = EXCLUDED.fit, judge = EXCLUDED.judge, check_opinion = EXCLUDED.check_opinion,
            split = EXCLUDED.split, grade_error = EXCLUDED.grade_error
        """,
        (
            eval_run_id,
            paper_id,
            topic,
            paper_revision,
            fit,
            opinion("judge"),
            opinion("check"),
            split,
            error,
        ),
    )


async def outcomes(conn: psycopg.AsyncConnection, eval_run_id: int) -> list[Outcome]:
    """Every label of the run with what the stages concluded for its paper and topic, cohort by
    cohort in the order the run took them."""
    rows = await (
        await conn.execute(
            """
            SELECT l.paper_id, l.topic, l.cohort, l.positive, r.classifier, r.uncertain,
                   r.keyword, r.error, r.fit, r.grade_error
            FROM eval_labels l LEFT JOIN eval_results r
              ON r.eval_run_id = l.eval_run_id AND r.paper_id = l.paper_id AND r.topic = l.topic
            WHERE l.eval_run_id = %s
            ORDER BY array_position(
                ARRAY['owner', 'provisional', 'legacy', 'silver', 'miss'], l.cohort),
                l.cohort, l.topic, l.paper_id
            """,
            (eval_run_id,),
        )
    ).fetchall()
    return [Outcome.model_validate(r) for r in rows]


async def results(conn: psycopg.AsyncConnection, eval_run_id: int) -> list[dict[str, Any]]:
    """The run's rows as stored, with each paper's title."""
    return await (
        await conn.execute(
            "SELECT r.*, p.title FROM eval_results r JOIN papers p ON p.id = r.paper_id "
            "WHERE r.eval_run_id = %s ORDER BY r.paper_id, r.topic",
            (eval_run_id,),
        )
    ).fetchall()


async def misses(conn: psycopg.AsyncConnection) -> list[tuple[str, str]]:
    """Every reported miss: (paper, topic)."""
    rows = await (
        await conn.execute("SELECT paper_id, topic FROM misses ORDER BY reported_at, id")
    ).fetchall()
    return [(r["paper_id"], r["topic"]) for r in rows]
