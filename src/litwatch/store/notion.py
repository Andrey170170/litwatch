"""The state the `notion` integration keeps: its database under the parent page, and what it last
wrote to each paper's page. Every row is keyed by the integration's name."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


@dataclass
class Database:
    """The Notion objects made under `parent_page`; None for one not made yet. `topics` maps
    every topic ever exported to `{name, column, view}`."""

    integration: str
    parent_page: str
    database_id: str | None = None
    data_source_id: str | None = None
    guide_page: str | None = None
    guide_hash: str | None = None
    all_view: str | None = None
    topics: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Page:
    """What was last written to a paper's page. `page_id` is None while its creation is under
    way; `topics` maps each topic it is or was listed for to `{grade, shown, why, since}`.
    `gone_at` is set once the page was found trashed or deleted in Notion."""

    paper_id: str
    added: date
    page_id: str | None = None
    properties_hash: str | None = None
    body_hash: str | None = None
    topics: dict[str, dict[str, Any]] = field(default_factory=dict)
    gone_at: datetime | None = None


_DATABASE = (
    "integration, parent_page, database_id, data_source_id, guide_page, guide_hash, all_view, "
    "topics"
)
_PAGE = "paper_id, added, page_id, properties_hash, body_hash, topics, gone_at"


async def database(conn: psycopg.AsyncConnection, integration: str) -> Database | None:
    row = await (
        await conn.execute(
            f"SELECT {_DATABASE} FROM notion_databases WHERE integration = %s", (integration,)
        )
    ).fetchone()
    return Database(**row) if row else None


async def save_database(conn: psycopg.AsyncConnection, db: Database) -> None:
    await conn.execute(
        f"""
        INSERT INTO notion_databases ({_DATABASE}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (integration) DO UPDATE SET
            parent_page = EXCLUDED.parent_page, database_id = EXCLUDED.database_id,
            data_source_id = EXCLUDED.data_source_id, guide_page = EXCLUDED.guide_page,
            guide_hash = EXCLUDED.guide_hash, all_view = EXCLUDED.all_view,
            topics = EXCLUDED.topics, updated_at = now()
        """,
        (db.integration, db.parent_page, db.database_id, db.data_source_id, db.guide_page,
         db.guide_hash, db.all_view, Jsonb(db.topics)),
    )  # fmt: skip


async def forget(conn: psycopg.AsyncConnection, integration: str) -> None:
    """Drop the integration's database row and every page row under it."""
    await conn.execute("DELETE FROM notion_databases WHERE integration = %s", (integration,))


async def page(conn: psycopg.AsyncConnection, integration: str, paper_id: str) -> Page | None:
    row = await (
        await conn.execute(
            f"SELECT {_PAGE} FROM notion_pages WHERE integration = %s AND paper_id = %s",
            (integration, paper_id),
        )
    ).fetchone()
    return Page(**row) if row else None


async def save_page(conn: psycopg.AsyncConnection, integration: str, p: Page) -> None:
    await conn.execute(
        f"""
        INSERT INTO notion_pages (integration, {_PAGE}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (integration, paper_id) DO UPDATE SET
            page_id = EXCLUDED.page_id, properties_hash = EXCLUDED.properties_hash,
            body_hash = EXCLUDED.body_hash, topics = EXCLUDED.topics, gone_at = EXCLUDED.gone_at,
            updated_at = now()
        """,
        (integration, p.paper_id, p.added, p.page_id, p.properties_hash, p.body_hash,
         Jsonb(p.topics), p.gone_at),
    )  # fmt: skip


async def rewrite_properties(conn: psycopg.AsyncConnection, integration: str) -> None:
    """Forget every page's properties hash, so each row is written again."""
    await conn.execute(
        "UPDATE notion_pages SET properties_hash = NULL WHERE integration = %s", (integration,)
    )


async def papers(
    conn: psycopg.AsyncConnection, integration: str, topics: list[str], grades: list[str]
) -> list[str]:
    """In id order, the papers not dismissed whose effective grade for one of `topics` is one of
    `grades`, and every paper that has a page already."""
    rows = await (
        await conn.execute(
            "SELECT g.paper_id FROM effective_grades g JOIN papers p ON p.id = g.paper_id "
            "WHERE p.dismissed_at IS NULL AND g.topic = ANY(%s) AND g.grade = ANY(%s) "
            "UNION SELECT paper_id FROM notion_pages WHERE integration = %s ORDER BY 1",
            (topics, grades, integration),
        )
    ).fetchall()
    return [r["paper_id"] for r in rows]
