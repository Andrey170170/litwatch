"""What analysis recorded: the analysis itself, and the Weight and Gist it gave from the
abstract (data.md: `analyses`, `weights`, `gists`)."""

from __future__ import annotations

from typing import Any

import psycopg

from litwatch.core import Analysis, Paper, TaxonomyClass, Weight
from litwatch.store.papers import COLUMNS, row_to_paper
from litwatch.store.taxonomy import row_to_class


async def due(
    conn: psycopg.AsyncConnection, runs: int, limit: int | None = None
) -> list[tuple[Paper, TaxonomyClass]]:
    """Papers whose current text is classified but not analysed, oldest first, each with the
    primary class of its newest classification (retired since or not). One that failed is
    tried again after the new ones, and after `runs` failed runs it is not tried at all: it
    waits for the owner. A dismissed paper is never analysed."""
    rows = await (
        await conn.execute(
            f"""
            SELECT {COLUMNS}, k.slug, k.name, k.description, k.parent, k.threshold
            FROM papers p
            JOIN LATERAL (SELECT primary_class FROM classifications
                          WHERE paper_id = p.id AND paper_revision = p.revision
                          ORDER BY id DESC LIMIT 1) c ON true
            JOIN classes k ON k.slug = c.primary_class
            LEFT JOIN step_failures f ON f.paper_id = p.id AND f.step = 'analysis'
                 AND f.paper_revision = p.revision
            WHERE p.dismissed_at IS NULL AND coalesce(f.failed_runs, 0) < %s
              AND NOT EXISTS (SELECT 1 FROM analyses a
                              WHERE a.paper_id = p.id AND a.paper_revision = p.revision)
            ORDER BY f.paper_id IS NOT NULL, coalesce(f.failed_runs, 0), p.fetched_at, p.id
            LIMIT %s
            """,
            (runs, limit),
        )
    ).fetchall()
    return [(row_to_paper(r), row_to_class(r)) for r in rows]


async def add(
    conn: psycopg.AsyncConnection, paper: Paper, primary: str, a: Analysis, model: str
) -> int:
    """Store the analysis with its Weight and Gist from the abstract. Returns the Gist's id."""
    await conn.execute(
        """
        INSERT INTO analyses (paper_id, paper_revision, primary_class, gist, weight,
                              weight_reason, contribution, releases_code, releases_weights, model)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            paper.id,
            paper.revision,
            primary,
            a.gist,
            a.weight.value,
            a.weight_reason,
            a.facets.contribution.value,
            a.facets.releases_code,
            a.facets.releases_weights,
            model,
        ),
    )
    await conn.execute(
        "INSERT INTO weights (paper_id, paper_revision, weight, source, reason, model) "
        "VALUES (%s, %s, %s, 'abstract', %s, %s)",
        (paper.id, paper.revision, a.weight.value, a.weight_reason, model),
    )
    row = await (
        await conn.execute(
            "INSERT INTO gists (paper_id, paper_revision, arxiv_version, text, source, model) "
            "VALUES (%s, %s, %s, %s, 'abstract', %s) RETURNING id",
            (paper.id, paper.revision, paper.version, a.gist, model),
        )
    ).fetchone()
    return row["id"]


async def current_weight(conn: psycopg.AsyncConnection, paper_id: str) -> Weight | None:
    """The paper's current Weight: the newest for its current revision, or None until the
    current text is analysed (pipeline.md, section 4: it is then treated as solid). The
    full-text queue (N3) refines this to the pass queued last for the current revision."""
    row = await weight_of(conn, paper_id)
    return Weight(row["weight"]) if row else None


async def weight_of(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The current Weight's row, `current_weight`'s, with its reason and source."""
    return await (
        await conn.execute(
            "SELECT w.weight, w.reason, w.source FROM weights w JOIN papers p ON p.id = w.paper_id "
            "AND p.revision = w.paper_revision WHERE w.paper_id = %s ORDER BY w.id DESC LIMIT 1",
            (paper_id,),
        )
    ).fetchone()


# The current Gist of paper `p` (an alias in the query using it), as a scalar subquery: the newest
# for its current revision. The full-text queue (N3) refines it as it does `current_weight`.
CURRENT_GIST = (
    "(SELECT g.text FROM gists g WHERE g.paper_id = p.id AND g.paper_revision = p.revision "
    "ORDER BY g.id DESC LIMIT 1)"
)
