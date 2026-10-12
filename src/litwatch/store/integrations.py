"""The state the built-in integrations keep of their own: Zotero items, stored PDFs, deep cards."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


async def _has_column(conn: psycopg.AsyncConnection, table: str, column: str) -> bool:
    row = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
            "table_schema = current_schema() AND table_name = %s AND column_name = %s) AS found",
            (table, column),
        )
    ).fetchone()
    return row["found"]


# -- zotero ----------------------------------------------------------------------------------------


async def zotero_item(conn: psycopg.AsyncConnection, paper_id: str) -> str | None:
    row = await (
        await conn.execute("SELECT item_key FROM zotero_items WHERE paper_id = %s", (paper_id,))
    ).fetchone()
    return row["item_key"] if row else None


async def set_zotero_item(conn: psycopg.AsyncConnection, paper_id: str, key: str | None) -> None:
    if key is None:
        await conn.execute("DELETE FROM zotero_items WHERE paper_id = %s", (paper_id,))
        return
    await conn.execute(
        "INSERT INTO zotero_items (paper_id, item_key) VALUES (%s, %s) ON CONFLICT (paper_id) "
        "DO UPDATE SET item_key = EXCLUDED.item_key, updated_at = now()",
        (paper_id, key),
    )


async def seed_zotero(conn: psycopg.AsyncConnection) -> int:
    """Take over the items 0.x made (`papers.zotero_key` on an adopted database), so they are
    updated and never made twice. Keys already known are kept."""
    if not await _has_column(conn, "papers", "zotero_key"):
        return 0
    cur = await conn.execute(
        "INSERT INTO zotero_items (paper_id, item_key) SELECT id, zotero_key FROM papers "
        "WHERE zotero_key IS NOT NULL ON CONFLICT (paper_id) DO NOTHING"
    )
    return cur.rowcount


# -- webdav ----------------------------------------------------------------------------------------


async def webdav_file(conn: psycopg.AsyncConnection, paper_id: str) -> str | None:
    row = await (
        await conn.execute(
            "SELECT attachment_key FROM webdav_files WHERE paper_id = %s", (paper_id,)
        )
    ).fetchone()
    return row["attachment_key"] if row else None


async def set_webdav_file(conn: psycopg.AsyncConnection, paper_id: str, key: str) -> None:
    await conn.execute(
        "INSERT INTO webdav_files (paper_id, attachment_key) VALUES (%s, %s) "
        "ON CONFLICT (paper_id) DO UPDATE SET attachment_key = EXCLUDED.attachment_key",
        (paper_id, key),
    )


async def seed_webdav(conn: psycopg.AsyncConnection) -> int:
    """The PDFs 0.x attached (`papers.zotero_attachment_key`), so none is stored twice."""
    if not await _has_column(conn, "papers", "zotero_attachment_key"):
        return 0
    cur = await conn.execute(
        "INSERT INTO webdav_files (paper_id, attachment_key) SELECT id, zotero_attachment_key "
        "FROM papers WHERE zotero_attachment_key IS NOT NULL ON CONFLICT (paper_id) DO NOTHING"
    )
    return cur.rowcount


# -- deep cards ------------------------------------------------------------------------------------


async def card(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The paper's deep card row: `card` is null while none has been written."""
    return await (
        await conn.execute(
            "SELECT paper_id, card, model, stats, error, attempts, attempted_at, written_at "
            "FROM deep_cards WHERE paper_id = %s",
            (paper_id,),
        )
    ).fetchone()


async def put_card(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    card: dict[str, Any],
    model: str,
    stats: dict[str, Any],
    trace: list[Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO deep_cards (paper_id, card, model, stats, trace, error, attempts,
                                attempted_at, written_at)
        VALUES (%s, %s, %s, %s, %s, NULL, 1, now(), now())
        ON CONFLICT (paper_id) DO UPDATE SET
            card = EXCLUDED.card, model = EXCLUDED.model, stats = EXCLUDED.stats,
            trace = EXCLUDED.trace, error = NULL, attempts = deep_cards.attempts + 1,
            attempted_at = now(), written_at = now()
        """,
        (paper_id, Jsonb(card), model, Jsonb(stats), Jsonb(trace)),
    )


async def card_failed(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    error: str,
    stats: dict[str, Any] | None = None,
    trace: list[Any] | None = None,
) -> None:
    """Record a failed attempt. A card written earlier stays."""
    await conn.execute(
        """
        INSERT INTO deep_cards (paper_id, stats, trace, error, attempts, attempted_at)
        VALUES (%s, %s, %s, %s, 1, now())
        ON CONFLICT (paper_id) DO UPDATE SET
            stats = EXCLUDED.stats, trace = EXCLUDED.trace, error = EXCLUDED.error,
            attempts = deep_cards.attempts + 1, attempted_at = now()
        """,
        (paper_id, Jsonb(stats or {}), Jsonb(trace or []), error),
    )


async def cards_attempted_since(conn: psycopg.AsyncConnection, since: datetime) -> int:
    row = await (
        await conn.execute(
            "SELECT count(*) AS n FROM deep_cards WHERE attempted_at >= %s", (since,)
        )
    ).fetchone()
    return row["n"]
