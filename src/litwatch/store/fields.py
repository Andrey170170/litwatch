"""Papers the owner put into a Radar or Pulse field themselves: "Important for <field>"
(spec, M7). The row is a membership like any other, marked `by_owner`, so the field's next
review sees it and its page lists it; triage never takes it away."""

from __future__ import annotations

import psycopg


async def add(conn: psycopg.AsyncConnection, paper_id: str, topic: str, revision: int) -> bool:
    """Make the paper a member of the field as the owner's own addition. A membership triage
    already made stays as it is. Returns whether a row was added."""
    cur = await conn.execute(
        """
        INSERT INTO memberships (paper_id, topic, topic_revision, by_owner)
        VALUES (%s, %s, %s, true)
        ON CONFLICT (paper_id, topic) DO NOTHING
        """,
        (paper_id, topic, revision),
    )
    return cur.rowcount > 0


async def remove(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> bool:
    """Take back the owner's addition: only a membership the owner made is dropped."""
    cur = await conn.execute(
        "DELETE FROM memberships WHERE paper_id = %s AND topic = %s AND by_owner",
        (paper_id, topic),
    )
    return cur.rowcount > 0


async def is_owners(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> bool:
    row = await (
        await conn.execute(
            "SELECT by_owner FROM memberships WHERE paper_id = %s AND topic = %s",
            (paper_id, topic),
        )
    ).fetchone()
    return bool(row and row["by_owner"])
