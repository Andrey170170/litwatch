"""What classification recorded: one row per classification, and the direct memberships."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Paper
from litwatch.core.classification import Membership, Source
from litwatch.store.papers import COLUMNS, row_to_paper

# The sources a classification writes, and replaces on the next one; the others are the owner's.
MACHINE = [Source.CLASSIFIER.value, Source.UNCERTAIN.value, Source.KEYWORD.value]


async def due(conn: psycopg.AsyncConnection, runs: int, limit: int | None = None) -> list[Paper]:
    """Papers with no classification of their current text, oldest first. One that failed is
    tried again after the new ones, so it never takes a new paper's place under `limit`, and
    after `runs` failed runs it is not tried at all: it waits for the owner. A dismissed paper
    is never classified."""
    rows = await (
        await conn.execute(
            f"""
            SELECT {COLUMNS} FROM papers p
            LEFT JOIN step_failures f ON f.paper_id = p.id AND f.step = 'classification'
                 AND f.paper_revision = p.revision
            WHERE p.dismissed_at IS NULL AND coalesce(f.failed_runs, 0) < %s
              AND NOT EXISTS (SELECT 1 FROM classifications c
                              WHERE c.paper_id = p.id AND c.paper_revision = p.revision)
            ORDER BY f.paper_id IS NOT NULL, coalesce(f.failed_runs, 0), p.fetched_at, p.id
            LIMIT %s
            """,
            (runs, limit),
        )
    ).fetchall()
    return [row_to_paper(r) for r in rows]


async def add(
    conn: psycopg.AsyncConnection,
    paper: Paper,
    taxonomy_revision: str,
    primary: str,
    response: dict[str, Any] | None,
    model: str,
) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
            "primary_class, response, model) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                paper.id,
                paper.revision,
                taxonomy_revision,
                primary,
                Jsonb(response) if response is not None else None,
                model,
            ),
        )
    ).fetchone()
    return row["id"]


async def _put(
    conn: psycopg.AsyncConnection, paper: Paper, m: Membership, class_version: str
) -> None:
    await conn.execute(
        """
        INSERT INTO memberships (paper_id, class, source, member, probability, class_version,
                                 paper_revision)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (paper_id, class, source) DO UPDATE SET
            member = EXCLUDED.member, probability = EXCLUDED.probability,
            class_version = EXCLUDED.class_version, paper_revision = EXCLUDED.paper_revision,
            recorded_at = now()
        """,
        (paper.id, m.slug, m.source.value, m.member, m.probability, class_version, paper.revision),
    )


async def replace(
    conn: psycopg.AsyncConnection,
    paper: Paper,
    found: list[Membership],
    versions: dict[str, str],
) -> None:
    """Make `found` the paper's classifier, uncertain and keyword memberships. The owner's rows
    stay. `versions` maps each class to the version the memberships were made under."""
    await conn.execute(
        "DELETE FROM memberships WHERE paper_id = %s AND source = ANY(%s)", (paper.id, MACHINE)
    )
    for m in found:
        await _put(conn, paper, m, versions[m.slug])


async def put_owner(
    conn: psycopg.AsyncConnection, paper: Paper, slug: str, member: bool, class_version: str
) -> None:
    """The owner's class label: in the class or out of it, whatever the other sources say."""
    m = Membership(slug=slug, source=Source.OWNER, member=member)
    await _put(conn, paper, m, class_version)


async def memberships(conn: psycopg.AsyncConnection, paper_id: str) -> list[Membership]:
    """Every direct membership row of the paper, whatever its source; `core.classification.
    standing` says which stand."""
    rows = await (
        await conn.execute(
            "SELECT class, source, member, probability FROM memberships WHERE paper_id = %s "
            "ORDER BY class, source",
            (paper_id,),
        )
    ).fetchall()
    return [
        Membership(
            slug=r["class"], source=r["source"], member=r["member"], probability=r["probability"]
        )
        for r in rows
    ]


async def current(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The newest classification of the paper's current text, or None."""
    return await (
        await conn.execute(
            "SELECT c.* FROM classifications c JOIN papers p ON p.id = c.paper_id "
            "AND p.revision = c.paper_revision WHERE c.paper_id = %s ORDER BY c.id DESC LIMIT 1",
            (paper_id,),
        )
    ).fetchone()
