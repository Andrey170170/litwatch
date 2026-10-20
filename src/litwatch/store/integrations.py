"""The state the built-in integrations keep of their own: Zotero items, stored PDFs, deep cards."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

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
    """The paper's deep card row: `card` is null while none has been written, `error` the last
    attempt's failure. A card says what it was written from: the paper revision, the arXiv
    version (null when unknown) and the text's source."""
    return await (
        await conn.execute(
            "SELECT paper_id, card, model, stats, error, written_at, paper_revision AS revision, "
            "arxiv_version, text_source AS source FROM deep_cards WHERE paper_id = %s",
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
    *,
    revision: int,
    version: int | None,
    source: str,
    pass_id: int | None,
) -> None:
    """Store the card, replacing any earlier one ("Write again"), labelled with the text it was
    written from."""
    await conn.execute(
        """
        INSERT INTO deep_cards (paper_id, card, model, stats, trace, written_at, paper_revision,
                                arxiv_version, text_source, pass_id)
        VALUES (%s, %s, %s, %s, %s, now(), %s, %s, %s, %s)
        ON CONFLICT (paper_id) DO UPDATE SET
            card = EXCLUDED.card, model = EXCLUDED.model, stats = EXCLUDED.stats,
            trace = EXCLUDED.trace, error = NULL, written_at = now(),
            paper_revision = EXCLUDED.paper_revision, arxiv_version = EXCLUDED.arxiv_version,
            text_source = EXCLUDED.text_source, pass_id = EXCLUDED.pass_id
        """,
        (paper_id, Jsonb(card), model, Jsonb(stats), Jsonb(trace), revision, version, source,
         pass_id),
    )  # fmt: skip


async def card_failed(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    error: str,
    stats: dict[str, Any] | None = None,
    trace: list[Any] | None = None,
) -> None:
    """Record why the paper's last attempt made no card. A card written earlier stays, and so
    do the last job's stats and trace unless new ones are given."""
    await conn.execute(
        """
        INSERT INTO deep_cards (paper_id, stats, trace, error)
        VALUES (%(p)s, coalesce(%(stats)s::jsonb, '{}'), coalesce(%(trace)s::jsonb, '[]'),
                %(error)s)
        ON CONFLICT (paper_id) DO UPDATE SET
            stats = coalesce(%(stats)s::jsonb, deep_cards.stats),
            trace = coalesce(%(trace)s::jsonb, deep_cards.trace),
            error = EXCLUDED.error
        """,
        {
            "p": paper_id,
            "stats": Jsonb(stats) if stats is not None else None,
            "trace": Jsonb(trace) if trace is not None else None,
            "error": error[:2000],
        },
    )


async def add_attempt(
    conn: psycopg.AsyncConnection, paper_id: str, pass_id: int, automatic: bool
) -> int:
    """A submission that may reach the service, recorded before it is sent."""
    row = await (
        await conn.execute(
            "INSERT INTO deep_attempts (paper_id, pass_id, automatic) VALUES (%s, %s, %s) "
            "RETURNING id",
            (paper_id, pass_id, automatic),
        )
    ).fetchone()
    return row["id"]


async def job_started(conn: psycopg.AsyncConnection, attempt_id: int, job_id: str) -> None:
    """The service accepted the attempt's job, now: it is followed until it ends, across a failed
    poll or a worker's death, and never submitted again meanwhile. A submission sent again
    after its answer was lost is followed from when it was accepted."""
    await conn.execute(
        "UPDATE deep_attempts SET job_id = %s, at = now() WHERE id = %s", (job_id, attempt_id)
    )


async def open_attempt(conn: psycopg.AsyncConnection, pass_id: int) -> dict[str, Any] | None:
    """The pass's attempt that has not ended: its job (null when the submission's answer never
    came), when it was submitted and whether it is automatic (not the owner's request)."""
    return await (
        await conn.execute(
            "SELECT id, job_id, at, automatic FROM deep_attempts "
            "WHERE pass_id = %s AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (pass_id,),
        )
    ).fetchone()


async def attempt_ended(
    conn: psycopg.AsyncConnection,
    attempt_id: int,
    usage: dict[str, Any] | None,
    error: str | None,
    failure: Literal["paper", "service"] | None = None,
) -> None:
    """The attempt's job ended, with a card or failed, and whose `failure` it was: its usage,
    or None when it is unknown (never zero for that)."""
    await conn.execute(
        "UPDATE deep_attempts SET ended_at = now(), usage = %(usage)s, error = %(error)s, "
        "failure = %(failure)s, service_failed_at = CASE WHEN %(failure)s = 'service' "
        "THEN now() ELSE service_failed_at END WHERE id = %(id)s AND ended_at IS NULL",
        {
            "usage": Jsonb(usage) if usage is not None else None,
            "error": error and error[:2000],
            "failure": failure,
            "id": attempt_id,
        },
    )


async def service_failed(conn: psycopg.AsyncConnection, attempt_id: int, error: str) -> None:
    """The service failed the attempt, which goes on: its submission's answer never came, or
    following its job failed."""
    await conn.execute(
        "UPDATE deep_attempts SET error = %s, service_failed_at = now() "
        "WHERE id = %s AND ended_at IS NULL",
        (error[:2000], attempt_id),
    )


async def paused_until(conn: psycopg.AsyncConnection, pause: timedelta) -> datetime | None:
    """When the Deep queue's pause ends: `pause` after the latest service failure, if that is
    still to come."""
    row = await (
        await conn.execute(
            "SELECT until FROM (SELECT max(service_failed_at) + %s AS until FROM deep_attempts) x "
            "WHERE until > now()",
            (pause,),
        )
    ).fetchone()
    return row["until"] if row else None


async def usage_since(conn: psycopg.AsyncConnection, since: datetime) -> dict[str, Any]:
    """The ended attempts since then, their list cost where known, and how many have their
    usage unknown: the subscription estimate, a lower bound while any is unknown."""
    return await (
        await conn.execute(
            "SELECT count(*) AS attempts, "
            "coalesce(sum((usage->>'list_cost')::numeric), 0)::float AS list_cost, "
            "count(*) FILTER (WHERE usage IS NULL) AS unknown "
            "FROM deep_attempts WHERE ended_at IS NOT NULL AND at >= %s",
            (since,),
        )
    ).fetchone()
