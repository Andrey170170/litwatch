"""Steps that failed on a paper's current text, counted by runs in a row (pipeline.md,
section 9). A success forgets the count; a new paper revision starts it again."""

from __future__ import annotations

from typing import Any, Literal

import psycopg

from litwatch.core import Paper

type Step = Literal["classification", "analysis"]


async def failed(conn: psycopg.AsyncConnection, paper: Paper, step: Step, error: str) -> int:
    """Count one more failed run of `step` on the paper's text. Returns the runs in a row."""
    row = await (
        await conn.execute(
            """
            INSERT INTO step_failures (paper_id, step, paper_revision, failed_runs, last_error)
            VALUES (%s, %s, %s, 1, %s)
            ON CONFLICT (paper_id, step) DO UPDATE SET
                failed_runs = CASE WHEN step_failures.paper_revision = EXCLUDED.paper_revision
                                   THEN step_failures.failed_runs + 1 ELSE 1 END,
                paper_revision = EXCLUDED.paper_revision, last_error = EXCLUDED.last_error,
                failed_at = now()
            RETURNING failed_runs
            """,
            (paper.id, step, paper.revision, error),
        )
    ).fetchone()
    return row["failed_runs"]


async def succeeded(conn: psycopg.AsyncConnection, paper_id: str, step: Step) -> None:
    await conn.execute(
        "DELETE FROM step_failures WHERE paper_id = %s AND step = %s", (paper_id, step)
    )


async def stuck(conn: psycopg.AsyncConnection, step: Step, runs: int) -> list[dict[str, Any]]:
    """Papers `step` failed on for at least `runs` runs in a row on their current text, with the
    last error; dismissed papers left out. Most failed first."""
    return await (
        await conn.execute(
            "SELECT f.paper_id, p.title, f.failed_runs, f.last_error, f.failed_at "
            "FROM step_failures f JOIN papers p ON p.id = f.paper_id "
            "AND p.revision = f.paper_revision AND p.dismissed_at IS NULL "
            "WHERE f.step = %s AND f.failed_runs >= %s "
            "ORDER BY f.failed_runs DESC, f.paper_id",
            (step, runs),
        )
    ).fetchall()


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
