"""What analysis recorded: the analysis itself, and the Weight and Gist it gave from the
abstract (data.md: `analyses`, `weights`, `gists`)."""

from __future__ import annotations

import psycopg

from litwatch.core import Analysis, Paper, TaxonomyClass
from litwatch.store.papers import COLUMNS, row_to_paper
from litwatch.store.taxonomy import row_to_class


async def due(
    conn: psycopg.AsyncConnection, limit: int | None = None
) -> list[tuple[Paper, TaxonomyClass]]:
    """Papers whose current text is classified but not analysed, oldest first, each with the
    primary class of its newest classification (retired since or not). One that failed is
    tried again on every run, after the new ones. A dismissed paper is never analysed."""
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
            WHERE p.dismissed_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM analyses a
                              WHERE a.paper_id = p.id AND a.paper_revision = p.revision)
            ORDER BY coalesce(f.failed_runs, 0), p.fetched_at, p.id LIMIT %s
            """,
            (limit,),
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
