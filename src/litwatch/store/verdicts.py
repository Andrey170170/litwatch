"""Verdicts, and the grading state of the candidates they answer."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Opinion, Verdict, current_verdict


def _verdict(row: dict[str, Any]) -> Verdict:
    return Verdict(
        id=row["id"],
        paper_id=row["paper_id"],
        topic=row["topic"],
        topic_revision=row["topic_revision"],
        paper_version=row["paper_version"],
        grade=row["grade"],
        split=row["split"],
        judge=Opinion.model_validate(row["judge"]) if row["judge"] else None,
        check=Opinion.model_validate(row["check_opinion"]) if row["check_opinion"] else None,
        migrated=row["migrated"],
        recorded_at=row["recorded_at"],
    )


def _json(opinion: Opinion | None) -> Jsonb | None:
    return Jsonb(opinion.model_dump(mode="json")) if opinion else None


async def add(conn: psycopg.AsyncConnection, verdict: Verdict) -> Verdict:
    """Record a grading as a new row; earlier rows stay as history."""
    row = await (
        await conn.execute(
            """
            INSERT INTO verdicts (paper_id, topic, topic_revision, paper_version, grade, split,
                                  judge, check_opinion, migrated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (
                verdict.paper_id,
                verdict.topic,
                verdict.topic_revision,
                verdict.paper_version,
                verdict.grade.value,
                verdict.split,
                _json(verdict.judge),
                _json(verdict.check),
                verdict.migrated,
            ),
        )
    ).fetchone()
    return _verdict(row)


async def history(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> list[Verdict]:
    rows = await (
        await conn.execute(
            "SELECT * FROM verdicts WHERE paper_id = %s AND topic = %s ORDER BY id",
            (paper_id, topic),
        )
    ).fetchall()
    return [_verdict(r) for r in rows]


async def current(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> Verdict | None:
    """The verdict that counts. A re-grade still pending, or one that failed, has no row and
    so displaces nothing."""
    return current_verdict(await history(conn, paper_id, topic))


async def mark_graded(
    conn: psycopg.AsyncConnection, paper_id: str, topic: str, topic_revision: int
) -> None:
    await conn.execute(
        "UPDATE candidates SET state = 'graded', last_error = NULL, updated_at = now() "
        "WHERE paper_id = %s AND topic = %s AND topic_revision = %s",
        (paper_id, topic, topic_revision),
    )


async def mark_failed(
    conn: psycopg.AsyncConnection, paper_id: str, topic: str, topic_revision: int, error: str
) -> None:
    await conn.execute(
        "UPDATE candidates SET failed_runs = failed_runs + 1, last_error = %s, updated_at = now() "
        "WHERE paper_id = %s AND topic = %s AND topic_revision = %s",
        (error, paper_id, topic, topic_revision),
    )
