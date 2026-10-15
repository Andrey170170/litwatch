"""The state the built-in integrations keep of their own: Zotero items, stored PDFs, deep cards."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Paper

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


# -- webdav ----------------------------------------------------------------------------------------


async def webdav_file(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The paper's PDF attachment, the Zotero item it hangs on, and its state: `pending` until
    litwatch's upload is known to be complete, `stored` after, `owner` for one litwatch leaves
    alone."""
    return await (
        await conn.execute(
            "SELECT item_key, attachment_key, state FROM webdav_files WHERE paper_id = %s",
            (paper_id,),
        )
    ).fetchone()


async def set_webdav_file(
    conn: psycopg.AsyncConnection, paper_id: str, item: str, key: str, state: str
) -> None:
    await conn.execute(
        "INSERT INTO webdav_files (paper_id, item_key, attachment_key, state) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (paper_id) DO UPDATE SET "
        "item_key = EXCLUDED.item_key, attachment_key = EXCLUDED.attachment_key, "
        "state = EXCLUDED.state, updated_at = now()",
        (paper_id, item, key, state),
    )


# -- deep cards ------------------------------------------------------------------------------------


async def card(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The paper's deep card row: `card` is null while none has been written, `job_id` set
    while a job is running for it."""
    return await (
        await conn.execute(
            "SELECT paper_id, card, model, stats, error, job_id, job_since, job_revision, "
            "job_version, job_event, job_automatic, written_at FROM deep_cards "
            "WHERE paper_id = %s",
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


async def job_started(
    conn: psycopg.AsyncConnection,
    paper: Paper,
    job_id: str,
    event_id: int,
    automatic: bool,
) -> None:
    """The service accepted a job for the paper: it is followed until it ends, across
    retries and restarts, and never submitted again meanwhile."""
    await conn.execute(
        """
        INSERT INTO deep_cards (paper_id, job_id, job_since, job_revision, job_version, job_event,
                                job_automatic)
        VALUES (%s, %s, now(), %s, %s, %s, %s)
        ON CONFLICT (paper_id) DO UPDATE SET
            job_id = EXCLUDED.job_id, job_since = EXCLUDED.job_since,
            job_revision = EXCLUDED.job_revision, job_version = EXCLUDED.job_version,
            job_event = EXCLUDED.job_event, job_automatic = EXCLUDED.job_automatic, error = NULL
        """,
        (paper.id, job_id, paper.revision, paper.version, event_id, automatic),
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
    conn: psycopg.AsyncConnection,
    paper_id: str,
    event_id: int,
    automatic: bool,
    *,
    due: bool = False,
) -> None:
    """The paper's text could not be had now: try again later. A paper already waiting keeps
    the time it began to, unless the owner now asks for a card it was waiting for by itself:
    their request takes over, with its own event and a fresh day, so later tries go by the
    request (no grade or library check, nothing counted toward the daily cap). `due` makes
    it the first to be tried again, rather than after the usual hour."""
    await conn.execute(
        """
        INSERT INTO deep_waiting (paper_id, event_id, automatic, tried_at)
        VALUES (%s, %s, %s, CASE WHEN %s THEN '-infinity'::timestamptz ELSE now() END)
        ON CONFLICT (paper_id) DO UPDATE SET
            tried_at = EXCLUDED.tried_at,
            event_id = CASE WHEN deep_waiting.automatic AND NOT EXCLUDED.automatic
                            THEN EXCLUDED.event_id ELSE deep_waiting.event_id END,
            since = CASE WHEN deep_waiting.automatic AND NOT EXCLUDED.automatic
                         THEN now() ELSE deep_waiting.since END,
            automatic = deep_waiting.automatic AND EXCLUDED.automatic
        """,
        (paper_id, event_id, automatic, due),
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
