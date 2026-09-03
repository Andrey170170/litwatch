"""PostgreSQL store: papers and runs. One async connection per unit of work."""

import json
from datetime import datetime
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from litwatch.models import Paper, ScreenResult


async def connect(dsn: str) -> psycopg.AsyncConnection[dict[str, Any]]:
    return await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row, autocommit=True)


async def apply_schema(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(resources.files("litwatch").joinpath("schema.sql").read_text())


async def upsert_papers(conn: psycopg.AsyncConnection, papers: list[Paper]) -> tuple[int, int]:
    """Insert new papers, refresh metadata on replacements. Returns (inserted, updated).

    Screening state is untouched on update: a replacement does not re-trigger routing.
    """
    inserted = updated = 0
    async with conn.cursor() as cur:
        for p in papers:
            await cur.execute(
                """
                INSERT INTO papers (id, title, abstract, authors, categories, primary_category,
                                    published, updated, url, keyword_hit)
                VALUES (%(id)s, %(title)s, %(abstract)s, %(authors)s, %(categories)s,
                        %(primary_category)s, %(published)s, %(updated)s, %(url)s, %(keyword_hit)s)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title, abstract = EXCLUDED.abstract,
                    authors = EXCLUDED.authors, categories = EXCLUDED.categories,
                    primary_category = EXCLUDED.primary_category,
                    updated = EXCLUDED.updated, url = EXCLUDED.url,
                    keyword_hit = EXCLUDED.keyword_hit
                RETURNING (xmax = 0) AS inserted
                """,
                {
                    **p.model_dump(),
                    "authors": Jsonb(p.authors),
                    "keyword_hit": _keyword_hit(p),
                },
            )
            row = await cur.fetchone()
            if row and row["inserted"]:
                inserted += 1
            else:
                updated += 1
    return inserted, updated


def _keyword_hit(paper: Paper) -> bool:
    from litwatch.screen import keyword_hit  # local import: screen imports models only

    return keyword_hit(paper)


async def pending(conn: psycopg.AsyncConnection, limit: int = 5000) -> list[Paper]:
    """Unscreened papers, oldest fetched first."""
    rows = await (
        await conn.execute(
            "SELECT * FROM papers WHERE screened_at IS NULL ORDER BY fetched_at LIMIT %s",
            (limit,),
        )
    ).fetchall()
    return [row_to_paper(r) for r in rows]


def row_to_paper(row: dict[str, Any]) -> Paper:
    return Paper(
        id=row["id"],
        title=row["title"],
        abstract=row["abstract"],
        authors=list(row["authors"] or []),
        categories=list(row["categories"] or []),
        primary_category=row.get("primary_category"),
        published=_iso(row.get("published")),
        updated=_iso(row.get("updated")),
        url=row.get("url"),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def store_screen(conn: psycopg.AsyncConnection, result: ScreenResult) -> None:
    await conn.execute(
        """
        UPDATE papers SET screened_at = now(), screen = %s, tier = %s,
                          prompt_version = %s, screen_error = NULL
        WHERE id = %s
        """,
        (
            Jsonb(result.model_dump(mode="json")),
            result.tier.value,
            result.prompt_version,
            result.paper_id,
        ),
    )


async def store_screen_error(conn: psycopg.AsyncConnection, paper_id: str, error: str) -> None:
    """Leaves the paper pending so the next run retries it."""
    await conn.execute("UPDATE papers SET screen_error = %s WHERE id = %s", (error, paper_id))


async def start_run(
    conn: psycopg.AsyncConnection, kind: str, window_from: datetime, window_to: datetime
) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO runs (kind, window_from, window_to) VALUES (%s, %s, %s) RETURNING id",
            (kind, window_from, window_to),
        )
    ).fetchone()
    assert row is not None
    return int(row["id"])


async def finish_run(conn: psycopg.AsyncConnection, run_id: int, **fields: Any) -> None:
    if "tiers" in fields:
        fields["tiers"] = Jsonb(fields["tiers"])
    assignments = ", ".join(f"{k} = %({k})s" for k in fields)
    await conn.execute(
        f"UPDATE runs SET finished_at = now(), {assignments} WHERE id = %(id)s",
        {**fields, "id": run_id},
    )


async def get_run(conn: psycopg.AsyncConnection, run_id: int) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,))).fetchone()


async def list_runs(conn: psycopg.AsyncConnection, limit: int = 20) -> list[dict[str, Any]]:
    return await (
        await conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT %s", (limit,))
    ).fetchall()


async def list_papers(
    conn: psycopg.AsyncConnection,
    tier: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Screened papers, newest screening first, with the screen JSON inline."""
    clauses, params = ["screened_at IS NOT NULL"], []
    if tier:
        clauses.append("tier = %s")
        params.append(tier)
    if since:
        clauses.append("screened_at >= %s")
        params.append(since)
    params.append(limit)
    rows = await (
        await conn.execute(
            f"""
            SELECT id, title, authors, categories, published, url, screened_at, tier,
                   keyword_hit, screen
            FROM papers WHERE {" AND ".join(clauses)}
            ORDER BY screened_at DESC LIMIT %s
            """,
            params,
        )
    ).fetchall()
    return rows


async def stats(conn: psycopg.AsyncConnection) -> dict[str, Any]:
    tiers = await (
        await conn.execute(
            "SELECT coalesce(tier, 'pending') AS tier, count(*) AS n FROM papers GROUP BY 1"
        )
    ).fetchall()
    total = await (await conn.execute("SELECT count(*) AS n FROM papers")).fetchone()
    return {"papers": total["n"] if total else 0, "tiers": {r["tier"]: r["n"] for r in tiers}}


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
