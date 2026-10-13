"""What triage and the keyword audit recorded: triage state, memberships and candidates."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Candidate, Paper
from litwatch.store.papers import COLUMNS, row_to_paper


async def untriaged(
    conn: psycopg.AsyncConnection, limit: int | None = None, attempts: int | None = None
) -> list[Paper]:
    """Papers no triage has answered for, oldest first. Failed ones are tried again on later
    runs, after the new ones, so a paper that keeps failing never takes a new paper's place
    under `limit`. After `attempts` failed runs a paper is not retried by itself: it waits
    for the owner (spec decision 24). A dismissed paper is never triaged."""
    rows = await (
        await conn.execute(
            f"SELECT {COLUMNS} FROM papers p LEFT JOIN triage t ON t.paper_id = p.id "
            "WHERE p.dismissed_at IS NULL AND (t.paper_id IS NULL "
            "   OR (t.state = 'failed' AND (%s::int IS NULL OR t.failed_runs < %s))) "
            "ORDER BY coalesce(t.failed_runs, 0), p.fetched_at, p.id LIMIT %s",
            (attempts, attempts, limit),
        )
    ).fetchall()
    return [row_to_paper(r) for r in rows]


async def dismiss(conn: psycopg.AsyncConnection, paper_id: str) -> bool:
    """Drop the paper from litwatch: never triaged or graded again, even after a rewrite. It
    stays in the collection, and in the archive."""
    cur = await conn.execute(
        "UPDATE papers SET dismissed_at = now() WHERE id = %s AND dismissed_at IS NULL",
        (paper_id,),
    )
    return cur.rowcount == 1


async def stuck(conn: psycopg.AsyncConnection, min_failed_runs: int) -> list[dict[str, Any]]:
    """Papers triage has failed on this many runs in a row, with the last error."""
    return await (
        await conn.execute(
            "SELECT paper_id, failed_runs, last_error FROM triage t "
            "JOIN papers p ON p.id = t.paper_id AND p.dismissed_at IS NULL "
            "WHERE state = 'failed' AND failed_runs >= %s ORDER BY failed_runs DESC, paper_id",
            (min_failed_runs,),
        )
    ).fetchall()


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
    """Replace the paper's memberships among the field topics that were asked about. One
    the owner made ("Important for <field>") is theirs and stays."""
    await conn.execute(
        "DELETE FROM memberships WHERE paper_id = %s AND topic = ANY(%s) AND NOT by_owner",
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
    candidate carrying both sources; rewritten text makes it pending again, and what was
    said about the old text (sources, reason) is dropped."""
    await conn.execute(
        """
        INSERT INTO candidates (paper_id, topic, topic_revision, paper_version, paper_revision,
                                by_triage, by_keyword, by_owner, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (paper_id, topic, topic_revision) DO UPDATE SET
            by_triage = EXCLUDED.by_triage
                OR (candidates.by_triage AND candidates.paper_revision = EXCLUDED.paper_revision),
            by_keyword = EXCLUDED.by_keyword
                OR (candidates.by_keyword AND candidates.paper_revision = EXCLUDED.paper_revision),
            by_owner = EXCLUDED.by_owner
                OR (candidates.by_owner AND candidates.paper_revision = EXCLUDED.paper_revision),
            reason = CASE WHEN candidates.paper_revision = EXCLUDED.paper_revision
                          THEN coalesce(EXCLUDED.reason, candidates.reason)
                          ELSE EXCLUDED.reason END,
            state = CASE WHEN candidates.paper_revision = EXCLUDED.paper_revision
                         THEN candidates.state ELSE 'pending' END,
            failed_runs = CASE WHEN candidates.paper_revision = EXCLUDED.paper_revision
                               THEN candidates.failed_runs ELSE 0 END,
            last_error = CASE WHEN candidates.paper_revision = EXCLUDED.paper_revision
                              THEN candidates.last_error END,
            paper_version = EXCLUDED.paper_version, paper_revision = EXCLUDED.paper_revision,
            updated_at = now()
        """,
        (
            candidate.paper_id,
            candidate.topic,
            candidate.topic_revision,
            candidate.paper_version,
            candidate.paper_revision,
            candidate.by_triage,
            candidate.by_keyword,
            candidate.by_owner,
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
            SELECT c.paper_id, c.topic, c.topic_revision, c.paper_version, c.paper_revision,
                   c.by_triage, c.by_keyword, c.by_owner, c.reason, c.state, c.failed_runs,
                   c.last_error, {COLUMNS}
            FROM candidates c JOIN papers p ON p.id = c.paper_id
            WHERE c.state = 'pending' AND c.failed_runs < %s AND p.dismissed_at IS NULL
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
