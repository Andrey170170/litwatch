"""What triage and the keyword audit recorded: triage state, memberships and candidates."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Candidate, Paper
from litwatch.store.papers import COLUMNS, row_to_paper


async def untriaged(conn: psycopg.AsyncConnection, limit: int | None = None) -> list[Paper]:
    """Papers no triage has answered for, oldest first; failed ones are tried again."""
    rows = await (
        await conn.execute(
            f"SELECT {COLUMNS} FROM papers p LEFT JOIN triage t ON t.paper_id = p.id "
            "WHERE t.paper_id IS NULL OR t.state = 'failed' ORDER BY p.fetched_at, p.id LIMIT %s",
            (limit,),
        )
    ).fetchall()
    return [row_to_paper(r) for r in rows]


async def mark_migrated(conn: psycopg.AsyncConnection) -> int:
    """Count every stored paper as triaged, so a first run starts from tonight's papers.
    Edits and migrations apply forward; older papers are reached by explicit re-runs."""
    cur = await conn.execute(
        "INSERT INTO triage (paper_id, paper_version, state) "
        "SELECT id, version, 'migrated' FROM papers ON CONFLICT (paper_id) DO NOTHING"
    )
    return cur.rowcount


async def store_done(
    conn: psycopg.AsyncConnection,
    paper: Paper,
    answers: dict[str, dict[str, Any]],
    revisions: dict[str, int],
    model: str | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO triage (paper_id, paper_version, state, answers, revisions, model)
        VALUES (%s, %s, 'done', %s, %s, %s)
        ON CONFLICT (paper_id) DO UPDATE SET
            paper_version = EXCLUDED.paper_version, state = 'done', answers = EXCLUDED.answers,
            revisions = EXCLUDED.revisions, model = EXCLUDED.model, last_error = NULL,
            triaged_at = now()
        """,
        (paper.id, paper.version, Jsonb(answers), Jsonb(revisions), model),
    )


async def store_failed(conn: psycopg.AsyncConnection, paper: Paper, error: str) -> None:
    await conn.execute(
        """
        INSERT INTO triage (paper_id, paper_version, state, failed_runs, last_error)
        VALUES (%s, %s, 'failed', 1, %s)
        ON CONFLICT (paper_id) DO UPDATE SET
            paper_version = EXCLUDED.paper_version, state = 'failed',
            failed_runs = triage.failed_runs + 1, last_error = EXCLUDED.last_error,
            triaged_at = now()
        """,
        (paper.id, paper.version, error),
    )


async def set_memberships(
    conn: psycopg.AsyncConnection, paper: Paper, asked: dict[str, int], member_of: set[str]
) -> None:
    """Replace the paper's memberships among the field topics that were asked about."""
    await conn.execute(
        "DELETE FROM memberships WHERE paper_id = %s AND topic = ANY(%s)",
        (paper.id, [slug for slug in asked if slug not in member_of]),
    )
    for slug in sorted(member_of):
        await conn.execute(
            """
            INSERT INTO memberships (paper_id, topic, topic_revision, paper_version)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (paper_id, topic) DO UPDATE SET
                topic_revision = EXCLUDED.topic_revision, paper_version = EXCLUDED.paper_version
            """,
            (paper.id, slug, asked[slug], paper.version),
        )


async def put_candidate(conn: psycopg.AsyncConnection, candidate: Candidate) -> None:
    """One row per paper, topic and topic revision. A paper found by both paths is one
    candidate carrying both sources; a new paper version makes it pending again."""
    await conn.execute(
        """
        INSERT INTO candidates (paper_id, topic, topic_revision, paper_version,
                                by_triage, by_keyword, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (paper_id, topic, topic_revision) DO UPDATE SET
            by_triage = candidates.by_triage OR EXCLUDED.by_triage,
            by_keyword = candidates.by_keyword OR EXCLUDED.by_keyword,
            reason = coalesce(EXCLUDED.reason, candidates.reason),
            state = CASE WHEN candidates.paper_version IS DISTINCT FROM EXCLUDED.paper_version
                         THEN 'pending' ELSE candidates.state END,
            failed_runs = CASE WHEN candidates.paper_version IS DISTINCT FROM EXCLUDED.paper_version
                               THEN 0 ELSE candidates.failed_runs END,
            paper_version = EXCLUDED.paper_version, updated_at = now()
        """,
        (
            candidate.paper_id,
            candidate.topic,
            candidate.topic_revision,
            candidate.paper_version,
            candidate.by_triage,
            candidate.by_keyword,
            candidate.reason,
        ),
    )


def _candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(**{k: row[k] for k in Candidate.model_fields})


async def pending_candidates(
    conn: psycopg.AsyncConnection, max_failed_runs: int
) -> list[tuple[Candidate, Paper]]:
    """Candidates waiting for a verdict, with their papers.

    One that failed `max_failed_runs` runs is left for the owner to see. One superseded by a
    candidate under a newer topic revision is dropped, so a late retry of an old version
    cannot overtake a newer verdict.
    """
    rows = await (
        await conn.execute(
            f"""
            SELECT c.paper_id, c.topic, c.topic_revision, c.paper_version, c.by_triage,
                   c.by_keyword, c.reason, c.state, c.failed_runs, c.last_error, {COLUMNS}
            FROM candidates c JOIN papers p ON p.id = c.paper_id
            WHERE c.state = 'pending' AND c.failed_runs < %s
              AND NOT EXISTS (SELECT 1 FROM candidates n
                              WHERE n.paper_id = c.paper_id AND n.topic = c.topic
                                AND n.topic_revision > c.topic_revision)
            ORDER BY c.created_at, c.paper_id, c.topic
            """,
            (max_failed_runs,),
        )
    ).fetchall()
    return [(_candidate(r), row_to_paper(r)) for r in rows]


async def candidates_for(conn: psycopg.AsyncConnection, paper_id: str) -> list[Candidate]:
    rows = await (
        await conn.execute(
            "SELECT * FROM candidates WHERE paper_id = %s ORDER BY topic, topic_revision",
            (paper_id,),
        )
    ).fetchall()
    return [_candidate(r) for r in rows]


async def ungraded(conn: psycopg.AsyncConnection, min_failed_runs: int) -> list[Candidate]:
    """Candidates grading gave up on. On a Watch topic a paper must not disappear behind a
    provider error, so these are shown to the owner as they are."""
    rows = await (
        await conn.execute(
            "SELECT * FROM candidates WHERE state = 'pending' AND failed_runs >= %s "
            "ORDER BY updated_at",
            (min_failed_runs,),
        )
    ).fetchall()
    return [_candidate(r) for r in rows]
