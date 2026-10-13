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


async def webdav_file(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The paper's PDF attachment and its state: `pending` until litwatch's upload is known to
    be complete, `stored` after, `owner` for one litwatch leaves alone."""
    return await (
        await conn.execute(
            "SELECT attachment_key, state FROM webdav_files WHERE paper_id = %s", (paper_id,)
        )
    ).fetchone()


async def set_webdav_file(
    conn: psycopg.AsyncConnection, paper_id: str, key: str, state: str
) -> None:
    await conn.execute(
        "INSERT INTO webdav_files (paper_id, attachment_key, state) VALUES (%s, %s, %s) "
        "ON CONFLICT (paper_id) DO UPDATE SET attachment_key = EXCLUDED.attachment_key, "
        "state = EXCLUDED.state, updated_at = now()",
        (paper_id, key, state),
    )


async def seed_webdav(conn: psycopg.AsyncConnection) -> int:
    """The PDFs 0.x attached (`papers.zotero_attachment_key`), so none is stored twice."""
    if not await _has_column(conn, "papers", "zotero_attachment_key"):
        return 0
    cur = await conn.execute(
        "INSERT INTO webdav_files (paper_id, attachment_key, state) "
        "SELECT id, zotero_attachment_key, 'stored' FROM papers "
        "WHERE zotero_attachment_key IS NOT NULL ON CONFLICT (paper_id) DO NOTHING"
    )
    return cur.rowcount


# -- deep cards ------------------------------------------------------------------------------------


async def card(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The paper's deep card row: `card` is null while none has been written, `job_id` set
    while a job is running for it."""
    return await (
        await conn.execute(
            "SELECT paper_id, card, model, stats, error, job_id, job_since, written_at "
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
    """Store the card; the job that made it is over."""
    await conn.execute(
        """
        INSERT INTO deep_cards (paper_id, card, model, stats, trace, written_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (paper_id) DO UPDATE SET
            card = EXCLUDED.card, model = EXCLUDED.model, stats = EXCLUDED.stats,
            trace = EXCLUDED.trace, error = NULL, job_id = NULL, job_since = NULL,
            written_at = now()
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
    """Record why a paper has no card; any job for it is over. A card written earlier stays,
    and so do the last job's stats and trace unless new ones are given."""
    await conn.execute(
        """
        INSERT INTO deep_cards (paper_id, stats, trace, error)
        VALUES (%(p)s, coalesce(%(stats)s::jsonb, '{}'), coalesce(%(trace)s::jsonb, '[]'),
                %(error)s)
        ON CONFLICT (paper_id) DO UPDATE SET
            stats = coalesce(%(stats)s::jsonb, deep_cards.stats),
            trace = coalesce(%(trace)s::jsonb, deep_cards.trace),
            error = EXCLUDED.error, job_id = NULL, job_since = NULL
        """,
        {
            "p": paper_id,
            "stats": Jsonb(stats) if stats is not None else None,
            "trace": Jsonb(trace) if trace is not None else None,
            "error": error,
        },
    )


async def job_started(conn: psycopg.AsyncConnection, paper_id: str, job_id: str) -> None:
    """The service accepted a job for the paper: it is followed until it ends, across
    retries and restarts, and never submitted again meanwhile."""
    await conn.execute(
        """
        INSERT INTO deep_cards (paper_id, job_id, job_since) VALUES (%s, %s, now())
        ON CONFLICT (paper_id) DO UPDATE SET
            job_id = EXCLUDED.job_id, job_since = EXCLUDED.job_since, error = NULL
        """,
        (paper_id, job_id),
    )


async def add_attempt(
    conn: psycopg.AsyncConnection, paper_id: str, event_id: int, automatic: bool
) -> None:
    """A submission that may reach the service, recorded before it is sent."""
    await conn.execute(
        "INSERT INTO deep_attempts (paper_id, event_id, automatic) VALUES (%s, %s, %s)",
        (paper_id, event_id, automatic),
    )


async def attempts(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    *,
    event_id: int | None = None,
    automatic: bool | None = None,
) -> int:
    """The paper's submissions, those for one event or of one kind only when asked."""
    row = await (
        await conn.execute(
            "SELECT count(*) AS n FROM deep_attempts WHERE paper_id = %s "
            "AND (%s::bigint IS NULL OR event_id = %s) "
            "AND (%s::boolean IS NULL OR automatic = %s)",
            (paper_id, event_id, event_id, automatic, automatic),
        )
    ).fetchone()
    return row["n"]


async def automatic_attempts_since(conn: psycopg.AsyncConnection, since: datetime) -> int:
    """Automatic submissions for any paper since then: what the daily cap counts."""
    row = await (
        await conn.execute(
            "SELECT count(*) AS n FROM deep_attempts WHERE automatic AND at >= %s", (since,)
        )
    ).fetchone()
    return row["n"]


async def wait_for_text(
    conn: psycopg.AsyncConnection, paper_id: str, event_id: int, automatic: bool
) -> None:
    """The paper's text could not be had now: try again later. A paper already waiting keeps
    the time it began to."""
    await conn.execute(
        "INSERT INTO deep_waiting (paper_id, event_id, automatic) VALUES (%s, %s, %s) "
        "ON CONFLICT (paper_id) DO UPDATE SET tried_at = now()",
        (paper_id, event_id, automatic),
    )


async def waiting(
    conn: psycopg.AsyncConnection, tried_before: datetime, limit: int
) -> list[dict[str, Any]]:
    """Papers waiting for their text, last tried before then, the longest-waiting first."""
    return await (
        await conn.execute(
            "SELECT paper_id, event_id, automatic, since FROM deep_waiting "
            "WHERE tried_at < %s ORDER BY since, paper_id LIMIT %s",
            (tried_before, limit),
        )
    ).fetchall()


async def touch_waiting(conn: psycopg.AsyncConnection, paper_id: str) -> None:
    await conn.execute("UPDATE deep_waiting SET tried_at = now() WHERE paper_id = %s", (paper_id,))


async def stop_waiting(conn: psycopg.AsyncConnection, paper_id: str) -> None:
    await conn.execute("DELETE FROM deep_waiting WHERE paper_id = %s", (paper_id,))
