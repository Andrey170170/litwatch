"""Verdicts: every grading of a paper for a Watch topic, each its own row."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Opinion, Verdict


def _verdict(row: dict[str, Any]) -> Verdict:
    return Verdict(
        id=row["id"],
        paper_id=row["paper_id"],
        topic=row["topic"],
        topic_revision=row["topic_revision"],
        paper_version=row["paper_version"],
        paper_revision=row["paper_revision"],
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
            INSERT INTO verdicts (paper_id, topic, topic_revision, paper_version,
                                  paper_revision, grade, split, judge, check_opinion, migrated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (
                verdict.paper_id,
                verdict.topic,
                verdict.topic_revision,
                verdict.paper_version,
                verdict.paper_revision,
                verdict.grade.value,
                verdict.split,
                _json(verdict.judge),
                _json(verdict.check),
                verdict.migrated,
            ),
        )
    ).fetchone()
    return _verdict(row)


async def for_paper(conn: psycopg.AsyncConnection, paper_id: str) -> list[Verdict]:
    """Every verdict on the paper, for every topic."""
    rows = await (
        await conn.execute("SELECT * FROM verdicts WHERE paper_id = %s ORDER BY id", (paper_id,))
    ).fetchall()
    return [_verdict(r) for r in rows]
