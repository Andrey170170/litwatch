"""The owner's labels, and the misses the owner reported."""

from __future__ import annotations

from typing import Any

from litwatch.core import Label
from litwatch.gates.database import ReadUnit, WriteUnit


def _label(row: dict[str, Any]) -> Label:
    return Label(**{k: row[k] for k in Label.model_fields})


async def set_label(unit: WriteUnit, label: Label) -> Label:
    """Store the owner's label. A paper keeps the origin it was first labelled under, since
    that says how it was chosen; the value and note are replaced. The time is the
    wall clock, as a reaction's is, so the newer word wins even inside one transaction."""
    row = await (
        await unit.execute(
            """
            INSERT INTO labels (paper_id, topic, value, origin, note, labelled_at)
            VALUES (%s, %s, %s, %s, %s, clock_timestamp())
            ON CONFLICT (paper_id, topic) DO UPDATE SET
                value = EXCLUDED.value, note = EXCLUDED.note, labelled_at = EXCLUDED.labelled_at
            RETURNING *
            """,
            (
                label.paper_id,
                label.topic,
                label.value.value,
                label.origin.value,
                label.note,
            ),
        )
    ).fetchone()
    return _label(row)


async def delete_label(unit: WriteUnit, paper_id: str, topic: str) -> None:
    await unit.execute("DELETE FROM labels WHERE paper_id = %s AND topic = %s", (paper_id, topic))


async def labels_of(unit: ReadUnit, paper_ids: list[str]) -> dict[str, list[Label]]:
    """The owner's labels on each of the papers that has any, by paper."""
    rows = await (
        await unit.execute("SELECT * FROM labels WHERE paper_id = ANY(%s)", (paper_ids,))
    ).fetchall()
    found: dict[str, list[Label]] = {}
    for r in rows:
        found.setdefault(r["paper_id"], []).append(_label(r))
    return found


async def labels(unit: ReadUnit, topic: str | None = None) -> list[Label]:
    rows = await (
        await unit.execute(
            "SELECT * FROM labels WHERE %s::text IS NULL OR topic = %s ORDER BY paper_id, topic",
            (topic, topic),
        )
    ).fetchall()
    return [_label(r) for r in rows]


async def labelled(unit: ReadUnit, paper_id: str, topic: str) -> bool:
    """Whether the paper has a label for the topic."""
    row = await (
        await unit.execute(
            "SELECT 1 FROM labels WHERE paper_id = %s AND topic = %s", (paper_id, topic)
        )
    ).fetchone()
    return row is not None


async def add_miss(unit: WriteUnit, paper_id: str, topic: str) -> tuple[int, bool]:
    """The miss of the paper for the topic, filed now if it was not: its id, and whether it is
    new."""
    row = await (
        await unit.execute(
            "INSERT INTO misses (paper_id, topic) VALUES (%s, %s) "
            "ON CONFLICT (paper_id, topic) DO NOTHING RETURNING id",
            (paper_id, topic),
        )
    ).fetchone()
    if row is not None:
        return row["id"], True
    row = await (
        await unit.execute(
            "SELECT id FROM misses WHERE paper_id = %s AND topic = %s", (paper_id, topic)
        )
    ).fetchone()
    assert row is not None  # the insert met it
    return row["id"], False
