"""Papers the owner put into a Radar or Pulse field themselves: "Important for <field>"
(spec, M7). The row is a membership like any other, so the field's next review sees it and
its page lists it; the owner's flag is theirs alone, as triage's is triage's, and the row
goes only when neither holds."""

from __future__ import annotations

import psycopg


async def add(conn: psycopg.AsyncConnection, paper_id: str, topic: str, revision: int) -> None:
    """Make the paper a member as the owner's own addition, now. A membership triage made
    keeps triage's flag; one the owner added before keeps its time."""
    await conn.execute(
        """
        INSERT INTO memberships (paper_id, topic, topic_revision, by_triage, by_owner,
                                 owner_added_at)
        VALUES (%s, %s, %s, false, true, now())
        ON CONFLICT (paper_id, topic) DO UPDATE SET
            by_owner = true,
            owner_added_at = CASE WHEN memberships.by_owner THEN memberships.owner_added_at
                                  ELSE now() END
        """,
        (paper_id, topic, revision),
    )


async def remove(conn: psycopg.AsyncConnection, paper_id: str, topic: str) -> bool:
    """Take back the owner's addition. What triage decided stands; the row goes only when
    triage never made it a member. Returns whether the owner's flag was set."""
    cur = await conn.execute(
        "UPDATE memberships SET by_owner = false, owner_added_at = NULL "
        "WHERE paper_id = %s AND topic = %s AND by_owner",
        (paper_id, topic),
    )
    await conn.execute(
        "DELETE FROM memberships WHERE paper_id = %s AND topic = %s "
        "AND NOT by_triage AND NOT by_owner",
        (paper_id, topic),
    )
    return cur.rowcount > 0
