"""Steps that failed on a paper's current text, counted by runs in a row (pipeline.md,
section 9). Only runs in which the model answered unusably count; a provider that did not
answer says nothing about the paper. A success forgets the count; a new paper revision starts
it again. Grading fails per paper and topic; the other steps per paper (`topic` '')."""

from __future__ import annotations

from typing import Any, Literal

import psycopg

from litwatch.core import Paper

type Step = Literal["classification", "analysis", "grading"]


async def failed(
    conn: psycopg.AsyncConnection,
    paper: Paper,
    step: Step,
    error: str,
    *,
    counts: bool,
    topic: str = "",
) -> int:
    """Record a run in which `step` failed on the paper's text. Only a failure that `counts`
    (the model answered, unusably) adds a failed run; any other keeps the count and records
    the error. Returns the failed runs in a row."""
    row = await (
        await conn.execute(
            """
            INSERT INTO step_failures (paper_id, step, topic, paper_revision, failed_runs,
                                       last_error)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (paper_id, step, topic) DO UPDATE SET
                failed_runs = EXCLUDED.failed_runs
                    + CASE WHEN step_failures.paper_revision = EXCLUDED.paper_revision
                           THEN step_failures.failed_runs ELSE 0 END,
                paper_revision = EXCLUDED.paper_revision, last_error = EXCLUDED.last_error,
                failed_at = now()
            RETURNING failed_runs
            """,
            (paper.id, step, topic, paper.revision, int(counts), error),
        )
    ).fetchone()
    return row["failed_runs"]


async def succeeded(
    conn: psycopg.AsyncConnection, paper_id: str, step: Step, topic: str = ""
) -> None:
    await conn.execute(
        "DELETE FROM step_failures WHERE paper_id = %s AND step = %s AND topic = %s",
        (paper_id, step, topic),
    )


async def reset(conn: psycopg.AsyncConnection, paper_id: str, step: Step, topic: str = "") -> None:
    """Forget the failed runs but keep the failure: the step is still pending on the paper,
    and the next run tries it again if nothing records it first."""
    await conn.execute(
        "UPDATE step_failures SET failed_runs = 0 WHERE paper_id = %s AND step = %s AND topic = %s",
        (paper_id, step, topic),
    )


async def stuck(conn: psycopg.AsyncConnection, step: Step, runs: int) -> list[dict[str, Any]]:
    """Papers `step` failed on for at least `runs` runs in a row on their current text, with the
    last error (and the topic, for grading); dismissed papers left out. Most failed first."""
    return await (
        await conn.execute(
            "SELECT f.paper_id, f.paper_revision AS revision, p.title, f.failed_runs, "
            "f.last_error, f.failed_at, nullif(f.topic, '') AS topic "
            "FROM step_failures f JOIN papers p ON p.id = f.paper_id "
            "AND p.revision = f.paper_revision AND p.dismissed_at IS NULL "
            "WHERE f.step = %s AND f.failed_runs >= %s "
            "ORDER BY f.failed_runs DESC, f.paper_id, f.topic",
            (step, runs),
        )
    ).fetchall()


async def waiting(
    conn: psycopg.AsyncConnection, paper: Paper, step: Step, runs: int, topic: str = ""
) -> bool:
    """Whether `step` failed at least `runs` runs in a row on this revision of the paper, which
    is not dismissed. Read under the paper's lock (`papers.lock`) to act on it."""
    row = await (
        await conn.execute(
            "SELECT 1 FROM step_failures f JOIN papers p ON p.id = f.paper_id "
            "AND p.revision = f.paper_revision AND p.dismissed_at IS NULL "
            "WHERE f.paper_id = %s AND f.step = %s AND f.topic = %s AND f.paper_revision = %s "
            "AND f.failed_runs >= %s",
            (paper.id, step, topic, paper.revision, runs),
        )
    ).fetchone()
    return row is not None


async def count_stuck(conn: psycopg.AsyncConnection, step: Step, runs: int) -> int:
    row = await (
        await conn.execute(
            "SELECT count(*) AS n FROM step_failures f JOIN papers p ON p.id = f.paper_id "
            "AND p.revision = f.paper_revision AND p.dismissed_at IS NULL "
            "WHERE f.step = %s AND f.failed_runs >= %s",
            (step, runs),
        )
    ).fetchone()
    return row["n"]
