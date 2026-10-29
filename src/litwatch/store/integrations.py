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
    *,
    revision: int,
    version: int | None,
    source: str,
    pass_id: int | None,
) -> None:
    """Store the card, replacing any earlier one ("Write again"), labelled with the text it was
    written from. A card written again has its process notes unseen: the owner's Acknowledge was
    for the card it replaces."""
    await conn.execute(
        """
        INSERT INTO deep_cards (paper_id, card, model, stats, written_at, paper_revision,
                                arxiv_version, text_source, pass_id)
        VALUES (%s, %s, %s, %s, now(), %s, %s, %s, %s)
        ON CONFLICT (paper_id) DO UPDATE SET
            card = EXCLUDED.card, model = EXCLUDED.model, stats = EXCLUDED.stats,
            error = NULL, written_at = now(),
            paper_revision = EXCLUDED.paper_revision, arxiv_version = EXCLUDED.arxiv_version,
            text_source = EXCLUDED.text_source, pass_id = EXCLUDED.pass_id,
            notes_acknowledged_at = NULL
        """,
        (paper_id, Jsonb(card), model, Jsonb(stats), revision, version, source, pass_id),
    )  # fmt: skip


async def card_failed(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    error: str,
    stats: dict[str, Any] | None = None,
) -> None:
    """Record why the paper's last attempt made no card. A card written earlier stays, and so
    do the last job's stats unless new ones are given."""
    await conn.execute(
        """
        INSERT INTO deep_cards (paper_id, stats, error)
        VALUES (%(p)s, coalesce(%(stats)s::jsonb, '{}'), %(error)s)
        ON CONFLICT (paper_id) DO UPDATE SET
            stats = coalesce(%(stats)s::jsonb, deep_cards.stats),
            error = EXCLUDED.error
        """,
        {
            "p": paper_id,
            "stats": Jsonb(stats) if stats is not None else None,
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
    """The service accepted the attempt's job, or was found to have it after its answer was
    lost: it is followed until it ends, across a failed poll or a worker's death, and never
    submitted again meanwhile."""
    await conn.execute("UPDATE deep_attempts SET job_id = %s WHERE id = %s", (job_id, attempt_id))


async def open_attempt(conn: psycopg.AsyncConnection, pass_id: int) -> dict[str, Any] | None:
    """The pass's attempt that has not ended: its job (null when the submission's answer never
    came) and when it was submitted."""
    return await (
        await conn.execute(
            "SELECT id, job_id, at FROM deep_attempts WHERE pass_id = %s AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (pass_id,),
        )
    ).fetchone()


async def attempt_ended(
    conn: psycopg.AsyncConnection,
    attempt_id: int,
    usage: dict[str, Any] | None,
    error: str | None,
    failure: Literal["paper", "service"] | None = None,
    trace: list[Any] | None = None,
) -> None:
    """The attempt's job ended, with a card or failed, and whose `failure` it was: its usage,
    or None when it is unknown (never zero for that), and its trace, kept for good, or None
    when there is none (its job never ran, was lost or never ended)."""
    await conn.execute(
        "UPDATE deep_attempts SET ended_at = now(), usage = %(usage)s, error = %(error)s, "
        "failure = %(failure)s, trace = %(trace)s, service_failed_at = CASE "
        "WHEN %(failure)s = 'service' THEN now() ELSE service_failed_at END "
        "WHERE id = %(id)s AND ended_at IS NULL",
        {
            "usage": Jsonb(usage) if usage is not None else None,
            "error": error and error[:2000],
            "failure": failure,
            "trace": Jsonb(trace) if trace is not None else None,
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


async def service_failure(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
    """The latest service failure of a Deep attempt, when no card was made after it: when, and
    its error while the attempt's error is the service's (null once the job it followed ended
    as the paper's failure)."""
    return await (
        await conn.execute(
            "SELECT service_failed_at AS at, "
            "CASE WHEN failure IS DISTINCT FROM 'paper' THEN error END AS error "
            "FROM deep_attempts "
            "WHERE service_failed_at > (SELECT coalesce(max(written_at), '-infinity') "
            "FROM deep_cards) ORDER BY service_failed_at DESC LIMIT 1"
        )
    ).fetchone()


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


async def attempts(conn: psycopg.AsyncConnection, paper_id: str) -> list[dict[str, Any]]:
    """The paper's Deep attempts, oldest first, without their traces: when each was submitted
    and ended, its job, error, whose failure it was and usage, whether it kept a trace
    (`traced`), and whether it wrote the paper's card (`wrote_card`)."""
    return await (
        await conn.execute(
            """
            SELECT a.id, a.at, a.ended_at, a.job_id, a.error, a.failure, a.usage, a.automatic,
                   a.trace IS NOT NULL AS traced,
                   coalesce(a.ended_at IS NOT NULL AND a.error IS NULL
                            AND a.pass_id = c.pass_id, false) AS wrote_card
            FROM deep_attempts a
            LEFT JOIN deep_cards c ON c.paper_id = a.paper_id AND c.card IS NOT NULL
            WHERE a.paper_id = %s ORDER BY a.id
            """,
            (paper_id,),
        )
    ).fetchall()


async def attempt_trace(conn: psycopg.AsyncConnection, attempt_id: int) -> list[Any] | None:
    """The attempt's trace; None when it kept none."""
    row = await (
        await conn.execute("SELECT trace FROM deep_attempts WHERE id = %s", (attempt_id,))
    ).fetchone()
    return row["trace"] if row else None


async def process_notes(
    conn: psycopg.AsyncConnection, since: datetime | None = None
) -> list[dict[str, Any]]:
    """The cards with process notes, newest first, written since `since` if given: the paper,
    its title, when the card was written and its notes."""
    return await (
        await conn.execute(
            """
            SELECT c.paper_id, p.title, c.written_at, c.card->'process_notes' AS notes
            FROM deep_cards c JOIN papers p ON p.id = c.paper_id
            WHERE jsonb_typeof(c.card->'process_notes') = 'array'
              AND jsonb_array_length(c.card->'process_notes') > 0
              AND (%(since)s::timestamptz IS NULL OR c.written_at >= %(since)s)
            ORDER BY c.written_at DESC, c.paper_id
            """,
            {"since": since},
        )
    ).fetchall()


# A card with process notes, as Needs you's Deep notes list it: of `deep_cards c`, its paper (`p`)
# not dismissed.
_NOTED = (
    "jsonb_typeof(c.card->'process_notes') = 'array' "
    "AND jsonb_array_length(c.card->'process_notes') > 0 AND p.dismissed_at IS NULL"
)


# How long acknowledged Deep notes stay in Needs you's Acknowledged section: a note never
# recovers as a failure does, so without a limit the section would only grow.
NOTES_KEPT_DAYS = 14


async def deep_notes(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    """Needs you's Deep notes: each card with process notes, oldest first, with its paper's
    title, when it was written, which names the card, and when the owner acknowledged its
    notes; acknowledged ones only for `NOTES_KEPT_DAYS` after. They wait on nothing, and count
    in no run status."""
    return await (
        await conn.execute(
            "SELECT c.paper_id, p.title, c.written_at, c.card->'process_notes' AS notes, "
            "c.notes_acknowledged_at AS acknowledged_at "
            f"FROM deep_cards c JOIN papers p ON p.id = c.paper_id WHERE {_NOTED} "
            "AND (c.notes_acknowledged_at IS NULL "
            "OR c.notes_acknowledged_at > now() - make_interval(days => %s)) "
            "ORDER BY c.written_at, c.paper_id",
            (NOTES_KEPT_DAYS,),
        )
    ).fetchall()


async def acknowledge_notes(
    conn: psycopg.AsyncConnection, items: list[tuple[str, datetime]]
) -> list[dict[str, Any]]:
    """The owner's Acknowledge on the named cards' process notes, each by its paper and when the
    card was written: a card written again since is not the one the owner saw, and stays unseen.
    One marked already keeps its time. Returns those marked."""
    if not items:
        return []
    ids, written = map(list, zip(*items, strict=True))
    return await (
        await conn.execute(
            "UPDATE deep_cards c "
            "SET notes_acknowledged_at = coalesce(c.notes_acknowledged_at, now()) "
            "FROM papers p, unnest(%(ids)s::text[], %(written)s::timestamptz[]) AS x (id, at) "
            f"WHERE c.paper_id = x.id AND c.written_at = x.at AND p.id = c.paper_id AND {_NOTED} "
            "RETURNING c.paper_id, c.written_at",
            {"ids": ids, "written": written},
        )
    ).fetchall()
