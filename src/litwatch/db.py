"""PostgreSQL store: papers and runs. One async connection per unit of work."""

import json
from datetime import datetime
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from litwatch.models import Paper, ScreenResult, Stage, StageRole, Usage


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


def parse_screen(raw: dict[str, Any]) -> ScreenResult:
    """Reads the stored screen JSON, including the pre-0.3 shape without `stages`."""
    if "stages" in raw:
        return ScreenResult.model_validate(raw)
    usage = [Usage.model_validate(u) for u in raw.get("usage", [])] or [Usage(model=raw["model"])]
    first = raw.get("first_stage") or raw["output"]
    stages = [Stage(role=StageRole.SCREEN, model=usage[0].model, output=first, usage=usage[0])]
    if raw.get("rescreened"):
        stages.append(
            Stage(
                role=StageRole.RESCREEN, model=raw["model"], output=raw["output"], usage=usage[-1]
            )
        )
    return ScreenResult(
        paper_id=raw["paper_id"], prompt_version=raw["prompt_version"], stages=stages
    )


async def migrate_screens(conn: psycopg.AsyncConnection) -> int:
    """Rewrites pre-0.3 screen JSON into the staged shape. Returns rows rewritten."""
    rows = await (
        await conn.execute(
            "SELECT id, screen FROM papers WHERE screen IS NOT NULL AND screen->'stages' IS NULL"
        )
    ).fetchall()
    for r in rows:
        await conn.execute(
            "UPDATE papers SET screen = %s WHERE id = %s",
            (Jsonb(parse_screen(r["screen"]).model_dump(mode="json")), r["id"]),
        )
    return len(rows)


async def unconfirmed_positives(conn: psycopg.AsyncConnection) -> list[tuple[Paper, ScreenResult]]:
    """Routed-tier papers whose screening predates the confirm stage."""
    rows = await (
        await conn.execute(
            """
            SELECT * FROM papers
            WHERE tier IN ('must_read', 'aware')
              AND (screen->'stages' IS NULL
                   OR NOT (screen->'stages' @> '[{"role": "confirm"}]'))
            ORDER BY published DESC
            """
        )
    ).fetchall()
    return [(row_to_paper(r), parse_screen(r["screen"])) for r in rows]


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
    tiers: list[str] | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Screened papers, newest screening first, with screen JSON and routing state inline."""
    clauses, params = ["screened_at IS NOT NULL"], []
    if tiers:
        clauses.append("tier = ANY(%s)")
        params.append(tiers)
    if since:
        clauses.append("screened_at >= %s")
        params.append(since)
    params.append(limit)
    rows = await (
        await conn.execute(
            f"""
            SELECT id, title, authors, categories, primary_category, published, url, fetched_at,
                   screened_at, tier, keyword_hit, screen,
                   zotero_key, zotero_attachment_key, routed_at, routed_tier, route_error
            FROM papers WHERE {" AND ".join(clauses)}
            ORDER BY screened_at DESC LIMIT %s
            """,
            params,
        )
    ).fetchall()
    return rows


async def run_positives(
    conn: psycopg.AsyncConnection, run_id: int
) -> list[tuple[Paper, ScreenResult]]:
    """Tier 1/2 papers screened during the given run, newest first."""
    rows = await (
        await conn.execute(
            """
            SELECT p.* FROM papers p, runs r
            WHERE r.id = %s AND p.tier IN ('must_read', 'aware')
              AND p.screened_at >= r.started_at
              AND p.screened_at <= coalesce(r.finished_at, now())
            ORDER BY p.published DESC
            """,
            (run_id,),
        )
    ).fetchall()
    return [(row_to_paper(r), parse_screen(r["screen"])) for r in rows]


async def positives_since(
    conn: psycopg.AsyncConnection, since: datetime
) -> list[tuple[Paper, ScreenResult]]:
    """Tier 1/2 papers screened since `since`, newest first (for ad hoc digests)."""
    rows = await (
        await conn.execute(
            """
            SELECT * FROM papers WHERE tier IN ('must_read', 'aware') AND screened_at >= %s
            ORDER BY published DESC
            """,
            (since,),
        )
    ).fetchall()
    return [(row_to_paper(r), parse_screen(r["screen"])) for r in rows]


async def store_digest(
    conn: psycopg.AsyncConnection, run_id: int, text: str, model: str | None
) -> None:
    await conn.execute(
        "UPDATE runs SET digest = %s, digest_model = %s WHERE id = %s", (text, model, run_id)
    )


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


async def routable(
    conn: psycopg.AsyncConnection, paper_id: str | None = None, limit: int = 200
) -> list[tuple[Paper, ScreenResult]]:
    """Tier 1/2 papers not yet routed, or routed at a different tier, or with a PDF still due."""
    clauses = [
        "tier IN ('must_read', 'aware')",
        """(routed_at IS NULL
            OR routed_tier IS DISTINCT FROM tier
            OR (tier = 'must_read' AND zotero_attachment_key IS NULL))""",
    ]
    params: list[Any] = []
    if paper_id:
        clauses.append("id = %s")
        params.append(paper_id)
    params.append(limit)
    rows = await (
        await conn.execute(
            f"SELECT * FROM papers WHERE {' AND '.join(clauses)} ORDER BY published DESC LIMIT %s",
            params,
        )
    ).fetchall()
    return [(row_to_paper(r), parse_screen(r["screen"])) for r in rows]


async def store_route(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    tier: str,
    zotero_key: str,
    attachment_key: str | None,
) -> None:
    await conn.execute(
        """
        UPDATE papers SET routed_at = now(), routed_tier = %s, zotero_key = %s,
                          zotero_attachment_key = coalesce(%s, zotero_attachment_key),
                          route_error = NULL
        WHERE id = %s
        """,
        (tier, zotero_key, attachment_key, paper_id),
    )


async def store_route_error(conn: psycopg.AsyncConnection, paper_id: str, error: str) -> None:
    await conn.execute("UPDATE papers SET route_error = %s WHERE id = %s", (error, paper_id))


async def get_paper(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM papers WHERE id = %s", (paper_id,))).fetchone()
