"""Steps that failed on a paper's current text, counted by runs in a row (pipeline.md,
section 9). Only runs in which the model answered unusably count; a provider that did not
answer says nothing about the paper. A step every model of its role refused stops at once,
counted as `FAILED_RUNS`. A success forgets the count; a new paper revision starts it again.
Grading fails per paper and topic; the other steps per paper (`topic` ''). The owner's
Acknowledge marks a stuck failure seen; a step left for the owner is not run again, so any later
failed run, after a retry or on a new revision, clears the mark."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

import psycopg

from litwatch.core import FAILED_RUNS, Paper

type Step = Literal["classification", "analysis", "grading"]


async def failed(
    conn: psycopg.AsyncConnection,
    paper: Paper,
    step: Step,
    error: str,
    *,
    counts: bool,
    topic: str = "",
    refused: Sequence[str] = (),
    stops: bool = False,
) -> int:
    """Record a run in which `step` failed on the paper's text. Only a failure that `counts`
    (the model answered, unusably) adds a failed run; any other keeps the count and records
    the error. `refused` are the roles whose every model refused on this text (a Focus side
    stays refused while the other side is asked); with `stops`, that refusal stops the step at
    once, for the owner, and the failure says so (`stopped_by_refusal`). Returns the failed runs
    in a row."""
    row = await (
        await conn.execute(
            """
            INSERT INTO step_failures (paper_id, step, topic, paper_revision, failed_runs,
                                       last_error, refused, stopped_by_refusal)
            VALUES (%(paper)s, %(step)s, %(topic)s, %(revision)s,
                    greatest(%(counts)s, %(floor)s), %(error)s, %(refused)s, %(stops)s)
            ON CONFLICT (paper_id, step, topic) DO UPDATE SET
                failed_runs = greatest(%(counts)s
                    + CASE WHEN step_failures.paper_revision = EXCLUDED.paper_revision
                           THEN step_failures.failed_runs ELSE 0 END, %(floor)s),
                paper_revision = EXCLUDED.paper_revision, last_error = EXCLUDED.last_error,
                refused = EXCLUDED.refused, stopped_by_refusal = EXCLUDED.stopped_by_refusal,
                failed_at = now(), acknowledged_at = NULL
            RETURNING failed_runs
            """,
            {
                "paper": paper.id,
                "step": step,
                "topic": topic,
                "revision": paper.revision,
                "counts": int(counts),
                "floor": FAILED_RUNS if stops else 0,
                "error": error,
                "refused": sorted(refused),
                "stops": stops,
            },
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
    """Forget the failed runs and the refusals but keep the failure: the step is still pending
    on the paper, and the next run tries it again, from the role's own model, if nothing
    records it first."""
    await conn.execute(
        "UPDATE step_failures SET failed_runs = 0, refused = '{}', stopped_by_refusal = false, "
        "acknowledged_at = NULL "
        "WHERE paper_id = %s AND step = %s AND topic = %s",
        (paper_id, step, topic),
    )


# Whether failure `f` of paper `p` waits for the owner's action, whatever its count: it is on
# the paper's current text, the paper is not dismissed, and a grading failure's topic is active
# (one paused, draft or unfollowed grades nothing, so nothing waits on it). Every view of the
# failures (the lists, the counts, a paper's detail) and the guarded actions read this one rule.
ACTIONABLE = (
    "(f.paper_revision = p.revision AND p.dismissed_at IS NULL AND (f.topic = '' OR EXISTS "
    "(SELECT 1 FROM topics t WHERE t.class = f.topic AND t.status = 'active')))"
)


async def stuck(conn: psycopg.AsyncConnection, step: Step, runs: int) -> list[dict[str, Any]]:
    """The failures of `step` that wait for the owner (`ACTIONABLE`) after at least `runs` runs
    in a row, with the last error (and the topic, for grading), the roles whose every model
    refused, whether that refusal stopped it, and when the owner acknowledged it. Most failed
    first."""
    return await (
        await conn.execute(
            "SELECT f.paper_id, f.paper_revision AS revision, p.title, f.failed_runs, "
            "f.last_error, f.refused, f.stopped_by_refusal, f.failed_at, "
            "nullif(f.topic, '') AS topic, "
            "f.acknowledged_at "
            "FROM step_failures f JOIN papers p ON p.id = f.paper_id "
            f"WHERE f.step = %s AND f.failed_runs >= %s AND {ACTIONABLE} "
            "ORDER BY f.failed_runs DESC, f.paper_id, f.topic",
            (step, runs),
        )
    ).fetchall()


async def stopped_between(
    conn: psycopg.AsyncConnection, since: datetime, until: datetime, runs: int
) -> dict[Step, int]:
    """Per step, the distinct papers it stopped on between `since` and `until`: those whose
    failure reached `runs` runs, or was stopped by a refusal, then. A step left for the owner is
    not run again, so its failure's time is when it stopped."""
    rows = await (
        await conn.execute(
            "SELECT step, count(DISTINCT paper_id) AS n FROM step_failures "
            "WHERE failed_runs >= %s AND failed_at BETWEEN %s AND %s GROUP BY step",
            (runs, since, until),
        )
    ).fetchall()
    return {r["step"]: r["n"] for r in rows}


async def waiting(
    conn: psycopg.AsyncConnection, paper: Paper, step: Step, runs: int, topic: str = ""
) -> bool:
    """Whether `step` failed at least `runs` runs in a row on this revision of the paper and
    waits for the owner (`ACTIONABLE`). Read in a change of the paper (`word.change`), under its
    locks and the taxonomy's, to act on it."""
    row = await (
        await conn.execute(
            "SELECT 1 FROM step_failures f JOIN papers p ON p.id = f.paper_id "
            "WHERE f.paper_id = %s AND f.step = %s AND f.topic = %s AND f.paper_revision = %s "
            f"AND f.failed_runs >= %s AND {ACTIONABLE}",
            (paper.id, step, topic, paper.revision, runs),
        )
    ).fetchone()
    return row is not None


async def count_stuck(
    conn: psycopg.AsyncConnection, step: Step, runs: int, *, unacknowledged: bool = False
) -> int:
    """How many failures of `step` wait for the owner; with `unacknowledged`, only those the
    owner has not acknowledged."""
    seen = "AND f.acknowledged_at IS NULL" if unacknowledged else ""
    row = await (
        await conn.execute(
            "SELECT count(*) AS n FROM step_failures f JOIN papers p ON p.id = f.paper_id "
            f"WHERE f.step = %s AND f.failed_runs >= %s AND {ACTIONABLE} {seen}",
            (step, runs),
        )
    ).fetchone()
    return row["n"]


# A failure as the owner saw it: step, paper, topic ('' but for grading), paper revision, and
# when it last failed, which names the failure: a step left for the owner is not run again, so
# a later failure, after a retry, has a later time.
type Item = tuple[Step, str, str, int, datetime]


async def acknowledge(
    conn: psycopg.AsyncConnection, items: list[Item], runs: int
) -> list[dict[str, Any]]:
    """The owner's Acknowledge on the named failures: each that still waits for them after at
    least `runs` runs, as named, is marked seen (one marked already keeps its time). Returns
    those marked, as named."""
    if not items:
        return []
    return await (
        await conn.execute(
            "UPDATE step_failures f SET acknowledged_at = coalesce(f.acknowledged_at, now()) "
            "FROM papers p, "
            "unnest(%s::text[], %s::text[], %s::text[], %s::int[], %s::timestamptz[]) "
            "AS x (step, paper_id, topic, rev, failed_at) "
            "WHERE f.step = x.step AND f.paper_id = x.paper_id AND f.topic = x.topic "
            "AND f.paper_revision = x.rev AND f.failed_at = x.failed_at AND p.id = f.paper_id "
            f"AND f.failed_runs >= %s AND {ACTIONABLE} "
            "RETURNING f.step, f.paper_id, nullif(f.topic, '') AS topic, "
            "f.paper_revision AS revision, f.failed_at",
            (*map(list, zip(*items, strict=True)), runs),
        )
    ).fetchall()
