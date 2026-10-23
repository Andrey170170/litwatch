"""The owner's queue (docs/plans/jobs.md): jobs, their claim by one worker at a time, and what
the Jobs page lists, Deep cards asked for read alongside from their passes."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core.fulltext import REQUESTED
from litwatch.store.runs import ALIVE

# The states each of the Jobs page's groups shows (DESIGN.md, Jobs); `discarded` is hidden.
NOT_RUN = ("offered", "stopped")
ACTIVE = ("tonight", "queued", "running")

# The session that runs a job, as `runs.start` records a run's.
_ME = "pg_backend_pid(), (SELECT backend_start FROM pg_stat_activity WHERE pid = pg_backend_pid())"


async def create(
    conn: psycopg.AsyncConnection, kind: str, scope: dict[str, Any], papers: int, state: str
) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO jobs (kind, scope, papers, state) VALUES (%s, %s, %s, %s) RETURNING id",
            (kind, Jsonb(scope), papers, state),
        )
    ).fetchone()
    return row["id"]


async def get(conn: psycopg.AsyncConnection, job_id: int) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))).fetchone()


async def process_of(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The paper's newest Process job not discarded."""
    return await (
        await conn.execute(
            "SELECT * FROM jobs WHERE kind = 'process' AND scope -> 'papers' = %s "
            "AND state <> 'discarded' ORDER BY id DESC LIMIT 1",
            (Jsonb([paper_id]),),
        )
    ).fetchone()


async def process(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any]:
    """Queue the paper's Process job (D1): the one already queued or running, the one that
    failed put back in the queue, or a new one. Asked again after a lost reply, it finds the
    job the first asking queued."""
    found = await process_of(conn, paper_id)
    if found is not None and found["state"] in ACTIVE:
        return found
    if found is not None and found["state"] != "done":
        moved = await move(conn, found["id"], (*NOT_RUN, "failed"), "queued")
        assert moved is not None  # the paper's lock is held: nothing moved it meanwhile
        return moved
    job = await get(conn, await create(conn, "process", {"papers": [paper_id]}, 1, "queued"))
    assert job is not None
    return job


async def move(
    conn: psycopg.AsyncConnection, job_id: int, sources: tuple[str, ...], to: str
) -> dict[str, Any] | None:
    """Move the job from one of `sources` to `to`; None when it is in none of them. Put in the
    queue or on tonight's run, it waits from now; a failed job put back starts over, its steps
    taking only what is still due."""
    return await (
        await conn.execute(
            """
            UPDATE jobs SET state = %(to)s, changed_at = now(),
                   queued_at = CASE WHEN %(to)s IN ('tonight', 'queued') THEN now()
                                    ELSE queued_at END,
                   done = CASE WHEN state = 'failed' THEN 0 ELSE done END,
                   counts = CASE WHEN state = 'failed' THEN '{}' ELSE counts END,
                   error = CASE WHEN state = 'failed' THEN NULL ELSE error END
            WHERE id = %(id)s AND state = ANY(%(sources)s) RETURNING *
            """,
            {"to": to, "id": job_id, "sources": list(sources)},
        )
    ).fetchone()


async def take_back(conn: psycopg.AsyncConnection) -> list[int]:
    """Put back in the queue every running job whose session is gone (its process died or its
    connection was lost): its steps pick only what is still due, so nothing done is redone."""
    rows = await (
        await conn.execute(
            f"""
            UPDATE jobs r SET state = 'queued', changed_at = now(),
                   backend_pid = NULL, backend_start = NULL
            WHERE state = 'running' AND NOT {ALIVE} RETURNING id
            """
        )
    ).fetchall()
    return [r["id"] for r in rows]


async def claim(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
    """The most urgent job this session may run now, marked running in its name: Process jobs
    first (the owner waits on them), then the oldest in the queue. Runnable are the queued
    jobs, those this session already runs, and those on tonight's run once a nightly run has
    finished after they were put there. Another session's claim in progress is skipped, so two
    workers never take one job."""
    return await (
        await conn.execute(
            f"""
            WITH chosen AS (
                SELECT j.id FROM jobs j
                WHERE j.state = 'queued'
                   OR (j.state = 'running' AND (j.backend_pid, j.backend_start) = ({_ME}))
                   OR (j.state = 'tonight' AND EXISTS (
                           SELECT 1 FROM runs n WHERE n.kind = 'nightly'
                           AND n.finished_at >= j.queued_at))
                ORDER BY j.kind = 'process' DESC, j.queued_at, j.id
                LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs j SET state = 'running', (backend_pid, backend_start) = ({_ME}),
                   changed_at = CASE WHEN j.state = 'running' THEN j.changed_at ELSE now() END
            FROM chosen WHERE j.id = chosen.id RETURNING j.*
            """
        )
    ).fetchone()


async def progress(
    conn: psycopg.AsyncConnection, job_id: int, done: int, counts: dict[str, int]
) -> dict[str, Any] | None:
    """Record how far the job got and what its steps recorded so far, for a job this session
    runs; None when it is no longer this session's."""
    return await (
        await conn.execute(
            f"""
            UPDATE jobs SET done = %s, counts = %s
            WHERE id = %s AND state = 'running' AND (backend_pid, backend_start) = ({_ME})
            RETURNING *
            """,
            (done, Jsonb(counts), job_id),
        )
    ).fetchone()


async def finish(
    conn: psycopg.AsyncConnection, job_id: int, state: str, error: str | None = None
) -> None:
    """End this session's run of the job as done or failed."""
    await conn.execute(
        f"""
        UPDATE jobs SET state = %s, error = %s, changed_at = now(),
               backend_pid = NULL, backend_start = NULL
        WHERE id = %s AND state = 'running' AND (backend_pid, backend_start) = ({_ME})
        """,
        (state, error, job_id),
    )


async def listing(conn: psycopg.AsyncConnection, done_limit: int) -> list[dict[str, Any]]:
    """Every job not discarded and every Deep card the owner asked for, as the Jobs page shows
    them: a job with its paper (a Process job's) and what its runs' calls cost, a run whose
    process died included (a call of unknown cost counts nothing, as in the month's spend); a
    card with its pass's state, mapped onto a job's. Only the newest `done_limit` of those done."""
    return await (
        await conn.execute(
            """
            WITH found AS (
                SELECT 'job' AS source, j.id, j.kind, j.state, j.papers, j.done, j.counts,
                       j.error, j.changed_at AS at, j.queued_at,
                       j.scope -> 'papers' ->> 0 AS paper_id, NULL::int AS failed_runs,
                       (SELECT sum((c.usage ->> 'cost')::numeric) FROM inference_calls c
                        JOIN runs r ON r.id = c.run_id WHERE r.job_id = j.id) AS cost
                FROM jobs j WHERE j.state <> 'discarded'
                UNION ALL
                SELECT 'card', f.id, 'deep',
                       CASE f.state WHEN 'awaiting_text' THEN 'queued' WHEN 'ready' THEN 'queued'
                                    ELSE f.state END,
                       1, CASE WHEN f.state = 'done' THEN 1 ELSE 0 END, '{}'::jsonb,
                       f.last_error, coalesce(f.finished_at, f.tried_at, f.queued_at),
                       f.queued_at, f.paper_id, f.failed_runs, NULL
                FROM fulltext_passes f
                WHERE f.kind = 'deep' AND f.reason = %(requested)s
            ), ranked AS (
                SELECT *, row_number() OVER (PARTITION BY state = 'done' ORDER BY at DESC, id DESC)
                          AS newest
                FROM found
            )
            SELECT x.*, p.title FROM ranked x LEFT JOIN papers p ON p.id = x.paper_id
            WHERE x.state <> 'done' OR x.newest <= %(done)s
            ORDER BY x.at DESC, x.id DESC
            """,
            {"requested": REQUESTED, "done": done_limit},
        )
    ).fetchall()
