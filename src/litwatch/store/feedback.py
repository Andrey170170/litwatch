"""The owner's labels."""

from __future__ import annotations

from typing import Any

import psycopg

from litwatch.core import Label


def _label(row: dict[str, Any]) -> Label:
    return Label(**{k: row[k] for k in Label.model_fields})


async def set_label(conn: psycopg.AsyncConnection, label: Label) -> Label:
    """Store the owner's label. A paper keeps the origin it was first labelled under, since
    that says how it was chosen; the value, grade and note are replaced. The time is the
    wall clock, as a reaction's is, so the newer word wins even inside one transaction."""
    row = await (
        await conn.execute(
            """
            INSERT INTO labels (paper_id, topic, value, grade, origin, provisional, note,
                                labelled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp())
            ON CONFLICT (paper_id, topic) DO UPDATE SET
                value = EXCLUDED.value, grade = EXCLUDED.grade, note = EXCLUDED.note,
                provisional = EXCLUDED.provisional, labelled_at = EXCLUDED.labelled_at,
                -- A bulk-accepted label was never read; a Calibrate draw of it is the sample.
                origin = CASE WHEN labels.provisional AND EXCLUDED.origin = 'sample'
                              THEN EXCLUDED.origin ELSE labels.origin END
            RETURNING *
            """,
            (
                label.paper_id,
                label.topic,
                label.value.value,
                label.grade.value if label.grade else None,
                label.origin.value,
                label.provisional,
                label.note,
            ),
        )
    ).fetchone()
    return _label(row)


async def delete_label(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> None:
    await conn.execute("DELETE FROM labels WHERE paper_id = %s AND topic = %s", (paper_id, topic))


async def labels_for(conn: psycopg.AsyncConnection, paper_id: str) -> list[Label]:
    rows = await (
        await conn.execute("SELECT * FROM labels WHERE paper_id = %s", (paper_id,))
    ).fetchall()
    return [_label(r) for r in rows]


async def labels(conn: psycopg.AsyncConnection, topic: str | None = None) -> list[Label]:
    rows = await (
        await conn.execute(
            "SELECT * FROM labels WHERE %s::text IS NULL OR topic = %s ORDER BY paper_id, topic",
            (topic, topic),
        )
    ).fetchall()
    return [_label(r) for r in rows]
