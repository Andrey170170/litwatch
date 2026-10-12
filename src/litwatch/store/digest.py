"""The reading side's records: takes, what is due for the writer and the next issue, issues."""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Grade, Take

# -- takes -----------------------------------------------------------------------------------------


def _take(row: dict[str, Any]) -> Take:
    return Take(**{k: row[k] for k in Take.model_fields})


async def add_take(conn: psycopg.AsyncConnection, take: Take) -> Take:
    row = await (
        await conn.execute(
            """
            INSERT INTO takes (paper_id, topic, paper_revision, topic_revision, grade, source, text)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (
                take.paper_id,
                take.topic,
                take.paper_revision,
                take.topic_revision,
                take.grade.value,
                take.source.value,
                take.text,
            ),
        )
    ).fetchone()
    return _take(row)


async def latest_take(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> Take | None:
    row = await (
        await conn.execute(
            "SELECT * FROM takes WHERE paper_id = %s AND topic = %s ORDER BY id DESC LIMIT 1",
            (paper_id, topic),
        )
    ).fetchone()
    return _take(row) if row else None


async def takes(conn: psycopg.AsyncConnection, ids: list[int]) -> dict[int, Take]:
    rows = await (await conn.execute("SELECT * FROM takes WHERE id = ANY(%s)", (ids,))).fetchall()
    return {r["id"]: _take(r) for r in rows}


# -- due -------------------------------------------------------------------------------------------


async def due_row(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> dict | None:
    return await (
        await conn.execute(
            "SELECT * FROM due WHERE paper_id = %s AND topic = %s", (paper_id, topic)
        )
    ).fetchone()


async def mark_due(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    topic: str,
    *,
    take: bool = False,
    issue: bool = False,
    fold: bool = False,
) -> None:
    """Add to what the pair is owed; nothing already owed is taken away here."""
    await conn.execute(
        """
        INSERT INTO due (paper_id, topic, for_take, for_issue, fold) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (paper_id, topic) DO UPDATE SET
            for_take = due.for_take OR EXCLUDED.for_take,
            for_issue = due.for_issue OR EXCLUDED.for_issue,
            fold = due.fold OR EXCLUDED.fold
        """,
        (paper_id, topic, take, issue, fold),
    )


async def settle_due(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    topic: str,
    *,
    take: bool | None = None,
    issue: bool | None = None,
    fold: bool | None = None,
) -> None:
    """Set the flags given, then drop the row once it owes nothing."""
    await conn.execute(
        """
        UPDATE due SET for_take = coalesce(%s, for_take), for_issue = coalesce(%s, for_issue),
                       fold = coalesce(%s, fold),
                       failed_runs = CASE WHEN %s IS FALSE THEN 0 ELSE failed_runs END,
                       last_error = CASE WHEN %s IS FALSE THEN NULL ELSE last_error END
        WHERE paper_id = %s AND topic = %s
        """,
        (take, issue, fold, take, take, paper_id, topic),
    )
    await conn.execute(
        "DELETE FROM due WHERE paper_id = %s AND topic = %s "
        "AND NOT (for_take OR for_issue OR fold)",
        (paper_id, topic),
    )


async def clear_due(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> None:
    await conn.execute("DELETE FROM due WHERE paper_id = %s AND topic = %s", (paper_id, topic))


async def take_failed(conn: psycopg.AsyncConnection, paper_id: str, topic: str, error: str) -> None:
    await conn.execute(
        "UPDATE due SET failed_runs = failed_runs + 1, last_error = %s "
        "WHERE paper_id = %s AND topic = %s",
        (error, paper_id, topic),
    )


async def due_for_take(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    return await (
        await conn.execute("SELECT * FROM due WHERE for_take ORDER BY since, paper_id, topic")
    ).fetchall()


async def due_for_issue(conn: psycopg.AsyncConnection, gave_up_after: int) -> list[dict[str, Any]]:
    """Pairs ready to enter an issue: their take is written, or the writer gave up on it."""
    return await (
        await conn.execute(
            "SELECT * FROM due WHERE for_issue AND (NOT for_take OR failed_runs >= %s) "
            "ORDER BY since, paper_id, topic",
            (gave_up_after,),
        )
    ).fetchall()


async def due_folds(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    return await (await conn.execute("SELECT * FROM due WHERE fold")).fetchall()


async def announced(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> bool:
    row = await (
        await conn.execute(
            "SELECT 1 FROM issue_entries WHERE paper_id = %s AND topic = %s LIMIT 1",
            (paper_id, topic),
        )
    ).fetchone()
    return row is not None


# -- issues ----------------------------------------------------------------------------------------


async def issue(conn: psycopg.AsyncConnection, kind: str, day: date) -> dict[str, Any] | None:
    return await (
        await conn.execute("SELECT * FROM issues WHERE kind = %s AND day = %s", (kind, day))
    ).fetchone()


async def open_issue(conn: psycopg.AsyncConnection, kind: str, day: date) -> dict[str, Any]:
    """The issue for the day, created when it does not exist yet."""
    await conn.execute(
        "INSERT INTO issues (kind, day) VALUES (%s, %s) ON CONFLICT (kind, day) DO NOTHING",
        (kind, day),
    )
    return await issue(conn, kind, day)


async def add_entry(
    conn: psycopg.AsyncConnection,
    issue_id: int,
    paper_id: str,
    topic: str,
    grade: Grade,
    take_id: int | None,
) -> bool:
    cur = await conn.execute(
        """
        INSERT INTO issue_entries (issue_id, paper_id, topic, grade, take_id, position)
        SELECT %s, %s, %s, %s, %s, coalesce(max(position), 0) + 1
        FROM issue_entries WHERE issue_id = %s
        ON CONFLICT DO NOTHING
        """,
        (issue_id, paper_id, topic, grade.value, take_id, issue_id),
    )
    return cur.rowcount == 1


async def add_folds(conn: psycopg.AsyncConnection, issue_id: int, counts: dict[str, int]) -> None:
    row = await (
        await conn.execute("SELECT folds FROM issues WHERE id = %s", (issue_id,))
    ).fetchone()
    folds = dict(row["folds"])
    for topic, n in counts.items():
        folds[topic] = folds.get(topic, 0) + n
    await conn.execute("UPDATE issues SET folds = %s WHERE id = %s", (Jsonb(folds), issue_id))


async def set_lede(conn: psycopg.AsyncConnection, issue_id: int, lede: str) -> None:
    await conn.execute("UPDATE issues SET lede = %s WHERE id = %s", (lede, issue_id))


async def entries(conn: psycopg.AsyncConnection, issue_id: int) -> list[dict[str, Any]]:
    """The issue's entries with their papers and takes, in the owner's topic order."""
    return await (
        await conn.execute(
            """
            SELECT e.paper_id, e.topic, e.grade, e.take_id, e.position, k.text AS take,
                   p.title, p.authors, p.published, t.name AS topic_name, t.position AS topic_pos,
                   g.grade AS grade_now
            FROM issue_entries e
            JOIN papers p ON p.id = e.paper_id
            JOIN topics t ON t.slug = e.topic
            LEFT JOIN takes k ON k.id = e.take_id
            LEFT JOIN effective_grades g ON g.paper_id = e.paper_id AND g.topic = e.topic
            WHERE e.issue_id = %s
            ORDER BY t.position, e.grade = 'relevant', e.position
            """,
            (issue_id,),
        )
    ).fetchall()


async def issues(conn: psycopg.AsyncConnection, limit: int = 30) -> list[dict[str, Any]]:
    return await (
        await conn.execute("SELECT * FROM issues ORDER BY day DESC, kind LIMIT %s", (limit,))
    ).fetchall()
