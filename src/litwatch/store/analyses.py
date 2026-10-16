"""What analysis recorded: the analysis itself, and the Weight and Gist it gave from the
abstract (data.md: `analyses`, `weights`, `gists`); and which Weight and Gist are current, the
full-text queue's (`store.fulltext`) before the abstract's."""

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


# Of a Weight or Gist row `x` of the paper's current revision, the current one first: from the
# full-text pass queued last, else the newest from the abstract (pipeline.md, section 6).
CURRENT_FIRST = "ORDER BY {x}.pass_id DESC NULLS LAST, {x}.id DESC"

# The current Weight of paper `p` (an alias in the query using it): of its current revision,
# chosen by `CURRENT_FIRST`, with its reason and source. Every read of a paper's Weight joins
# it, as `LEFT JOIN LATERAL ({CURRENT_WEIGHT}) w ON true`.
CURRENT_WEIGHT = (
    "SELECT x.weight, x.reason, x.source FROM weights x WHERE x.paper_id = p.id "
    "AND x.paper_revision = p.revision " + CURRENT_FIRST.format(x="x") + " LIMIT 1"
)


async def current_weight(conn: psycopg.AsyncConnection, paper_id: str) -> Weight | None:
    """The paper's current Weight for its current revision, or None until the current text is
    analysed or read (pipeline.md, section 4: it is then treated as solid)."""
    row = await weight_of(conn, paper_id)
    return Weight(row["weight"]) if row else None


async def weight_of(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The current Weight's row, `current_weight`'s, with its reason and source."""
    return await (
        await conn.execute(
            f"SELECT w.weight, w.reason, w.source FROM papers p "
            f"JOIN LATERAL ({CURRENT_WEIGHT}) w ON true WHERE p.id = %s",
            (paper_id,),
        )
    ).fetchone()


# The current Gist of paper `p` (an alias in the query using it), as a scalar subquery of the
# column `{col}`, as `current_weight` chooses.
_GIST = (
    "(SELECT g.{col} FROM gists g WHERE g.paper_id = p.id AND g.paper_revision = p.revision "
    + CURRENT_FIRST.format(x="g")
    + " LIMIT 1)"
)
CURRENT_GIST = _GIST.format(col="text")
CURRENT_GIST_ID = _GIST.format(col="id")
CURRENT_GIST_SOURCE = _GIST.format(col="source")


async def current_gist_id(conn: psycopg.AsyncConnection, paper_id: str) -> int | None:
    row = await (
        await conn.execute(
            f"SELECT {CURRENT_GIST_ID} AS id FROM papers p WHERE p.id = %s", (paper_id,)
        )
    ).fetchone()
    return row["id"] if row else None


async def abstract_gists(conn: psycopg.AsyncConnection, ids: list[str]) -> dict[str, str]:
    """The newest Gist written from each paper's abstract, of its current revision."""
    rows = await (
        await conn.execute(
            "SELECT DISTINCT ON (g.paper_id) g.paper_id, g.text FROM gists g "
            "JOIN papers p ON p.id = g.paper_id AND p.revision = g.paper_revision "
            "WHERE g.paper_id = ANY(%s) AND g.source = 'abstract' ORDER BY g.paper_id, g.id DESC",
            (ids,),
        )
    ).fetchall()
    return {r["paper_id"]: r["text"] for r in rows}
