"""Labels, the regression set, and evaluation runs with their results."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Label, Opinion


def _label(row: dict[str, Any]) -> Label:
    return Label(**{k: row[k] for k in Label.model_fields})


async def set_label(conn: psycopg.AsyncConnection, label: Label) -> Label:
    """Store the owner's label. A paper keeps the origin it was first labelled under, since
    that says how it was chosen; the value, grade and note are replaced."""
    row = await (
        await conn.execute(
            """
            INSERT INTO labels (paper_id, topic, value, grade, origin, provisional, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (paper_id, topic) DO UPDATE SET
                value = EXCLUDED.value, grade = EXCLUDED.grade, note = EXCLUDED.note,
                provisional = EXCLUDED.provisional, labelled_at = now()
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


async def add_provisional(conn: psycopg.AsyncConnection, label: Label) -> bool:
    """Add a label accepted in bulk. Never replaces a label that is already there."""
    cur = await conn.execute(
        """
        INSERT INTO labels (paper_id, topic, value, grade, origin, provisional, note)
        VALUES (%s, %s, %s, %s, %s, true, %s) ON CONFLICT (paper_id, topic) DO NOTHING
        """,
        (
            label.paper_id,
            label.topic,
            label.value.value,
            label.grade.value if label.grade else None,
            label.origin.value,
            label.note,
        ),
    )
    return cur.rowcount == 1


async def delete_label(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> None:
    await conn.execute("DELETE FROM labels WHERE paper_id = %s AND topic = %s", (paper_id, topic))


async def labels(conn: psycopg.AsyncConnection, topic: str | None = None) -> list[Label]:
    rows = await (
        await conn.execute(
            "SELECT * FROM labels WHERE %s::text IS NULL OR topic = %s ORDER BY paper_id, topic",
            (topic, topic),
        )
    ).fetchall()
    return [_label(r) for r in rows]


async def add_regression(
    conn: psycopg.AsyncConnection, paper_id: str, topic: str, tier: int, source: str
) -> None:
    await conn.execute(
        "INSERT INTO regression (paper_id, topic, tier, source) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (paper_id, topic) DO UPDATE SET tier = EXCLUDED.tier, "
        "source = EXCLUDED.source",
        (paper_id, topic, tier, source),
    )


async def regression(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    return await (
        await conn.execute("SELECT * FROM regression ORDER BY paper_id, topic")
    ).fetchall()


# -- evaluation runs -------------------------------------------------------------------------------


async def start_eval(
    conn: psycopg.AsyncConnection,
    kind: str,
    run_id: int,
    models: dict[str, Any],
    topics: dict[str, int],
    params: dict[str, Any],
    note: str | None,
) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO eval_runs (kind, run_id, models, topics, params, note) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (kind, run_id, Jsonb(models), Jsonb(topics), Jsonb(params), note),
        )
    ).fetchone()
    return row["id"]


async def finish_eval(conn: psycopg.AsyncConnection, eval_run_id: int) -> None:
    await conn.execute("UPDATE eval_runs SET finished_at = now() WHERE id = %s", (eval_run_id,))


async def get_eval(conn: psycopg.AsyncConnection, eval_run_id: int) -> dict[str, Any] | None:
    return await (
        await conn.execute(
            "SELECT e.*, r.cost, r.counts FROM eval_runs e LEFT JOIN runs r ON r.id = e.run_id "
            "WHERE e.id = %s",
            (eval_run_id,),
        )
    ).fetchone()


async def latest_eval(conn: psycopg.AsyncConnection, kind: str) -> dict[str, Any] | None:
    return await (
        await conn.execute(
            "SELECT * FROM eval_runs WHERE kind = %s AND finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (kind,),
        )
    ).fetchone()


async def put_triage_result(
    conn: psycopg.AsyncConnection,
    eval_run_id: int,
    paper_id: str,
    paper_version: int | None,
    topic: str,
    yes: bool | None,
    why: str | None,
    keyword: str | None,
    error: str | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO eval_results (eval_run_id, paper_id, paper_version, topic, triage_yes,
                                  triage_why, keyword, error)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (eval_run_id, paper_id, paper_version, topic, yes, why, keyword, error),
    )


async def put_grade_result(
    conn: psycopg.AsyncConnection,
    eval_run_id: int,
    paper_id: str,
    topic: str,
    judge: Opinion | None,
    check: Opinion | None,
    grade: str | None,
    split: bool | None,
    error: str | None,
) -> None:
    await conn.execute(
        """
        UPDATE eval_results SET judge = %s, check_opinion = %s, grade = %s, split = %s,
               error = coalesce(%s, error)
        WHERE eval_run_id = %s AND paper_id = %s AND topic = %s
        """,
        (
            Jsonb(judge.model_dump(mode="json")) if judge else None,
            Jsonb(check.model_dump(mode="json")) if check else None,
            grade,
            split,
            error,
            eval_run_id,
            paper_id,
            topic,
        ),
    )


async def results(conn: psycopg.AsyncConnection, eval_run_id: int) -> list[dict[str, Any]]:
    return await (
        await conn.execute(
            "SELECT * FROM eval_results WHERE eval_run_id = %s ORDER BY paper_id, topic",
            (eval_run_id,),
        )
    ).fetchall()


async def put_sample(
    conn: psycopg.AsyncConnection, eval_run_id: int, drawn: list[tuple[str, str]]
) -> None:
    """Store the drawn papers as (paper id, stratum), in the order given."""
    for position, (paper_id, stratum) in enumerate(drawn):
        await conn.execute(
            "INSERT INTO eval_samples (eval_run_id, paper_id, stratum, position) "
            "VALUES (%s, %s, %s, %s)",
            (eval_run_id, paper_id, stratum, position),
        )


async def sample(conn: psycopg.AsyncConnection, eval_run_id: int) -> list[dict[str, Any]]:
    return await (
        await conn.execute(
            "SELECT paper_id, stratum, position FROM eval_samples WHERE eval_run_id = %s "
            "ORDER BY position",
            (eval_run_id,),
        )
    ).fetchall()
