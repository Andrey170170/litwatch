"""Topics and their revisions."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Status, Topic, TopicKind


def _topic(row: dict[str, Any]) -> Topic:
    return Topic(
        slug=row["slug"],
        name=row["name"],
        kind=row["kind"],
        subject=row["subject"],
        rubric=row["rubric"],
        interests=row["interests"],
        keywords=list(row["keywords"] or []),
        status=row["status"],
        revision=row["revision"],
        position=row["position"],
        colour=row["colour"],
    )


# Each topic gets the first of these not in use (docs/v1/DESIGN.md, topic colours).
COLOURS = (
    "#2f5d50",
    "#8a3b2e",
    "#3b4f8a",
    "#a2661e",
    "#5a6b3a",
    "#7a4f8a",
    "#2f6b7a",
    "#8a6a3b",
)


async def _placement(conn: psycopg.AsyncConnection, topic: Topic) -> tuple[int, str]:
    """A new topic goes last, in the first colour not in use (cycling past eight)."""
    rows = await (await conn.execute("SELECT position, colour FROM topics")).fetchall()
    position = max((r["position"] or 0 for r in rows), default=0) + 1
    used = {r["colour"] for r in rows}
    free = [c for c in COLOURS if c not in used]
    return position, free[0] if free else COLOURS[len(rows) % len(COLOURS)]


async def save(conn: psycopg.AsyncConnection, topic: Topic) -> Topic:
    """Store the topic and return it with its revision.

    The revision goes up only when what a model sees of the topic changed; a rename or a
    status change keeps it. Edits apply forward: nothing recorded earlier is touched.
    """
    async with conn.transaction():
        current = await (
            await conn.execute(
                "SELECT version, revision FROM topics WHERE slug = %s FOR UPDATE", (topic.slug,)
            )
        ).fetchone()
        changed = current is None or current["version"] != topic.version
        revision = (current["revision"] if current else 0) + int(changed)
        position, colour = topic.position, topic.colour
        if current is None and (position is None or colour is None):
            placed = await _placement(conn, topic)
            position, colour = position or placed[0], colour or placed[1]
        cur = await conn.execute(
            """
            INSERT INTO topics (slug, name, kind, subject, rubric, interests, keywords, status,
                                version, revision, position, colour)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name, kind = EXCLUDED.kind, subject = EXCLUDED.subject,
                rubric = EXCLUDED.rubric, interests = EXCLUDED.interests,
                keywords = EXCLUDED.keywords, status = EXCLUDED.status,
                version = EXCLUDED.version, revision = EXCLUDED.revision,
                position = coalesce(EXCLUDED.position, topics.position),
                colour = coalesce(EXCLUDED.colour, topics.colour), updated_at = now()
            RETURNING position, colour
            """,
            (
                topic.slug,
                topic.name,
                topic.kind.value,
                topic.subject,
                topic.rubric,
                topic.interests,
                topic.keywords,
                topic.status.value,
                topic.version,
                revision,
                position,
                colour,
            ),
        )
        placed = await cur.fetchone()
        if changed:
            content = topic.model_dump(
                mode="json", exclude={"revision", "status", "name", "position", "colour"}
            )
            await conn.execute(
                "INSERT INTO topic_versions (topic, revision, hash, content) "
                "VALUES (%s, %s, %s, %s)",
                (topic.slug, revision, topic.version, Jsonb(content)),
            )
    return topic.model_copy(
        update={"revision": revision, "position": placed["position"], "colour": placed["colour"]}
    )


async def all(conn: psycopg.AsyncConnection, status: Status | None = Status.ACTIVE) -> list[Topic]:
    """Topics with the given status (every topic for None), Watch first, then in the owner's
    order."""
    rows = await (
        await conn.execute(
            "SELECT * FROM topics WHERE %s::text IS NULL OR status = %s "
            "ORDER BY array_position(ARRAY['watch', 'radar', 'pulse'], kind), position, slug",
            (status.value if status else None, status.value if status else None),
        )
    ).fetchall()
    return [_topic(r) for r in rows]


async def kind_for_update(conn: psycopg.AsyncConnection, slug: str) -> TopicKind | None:
    """The topic's kind, locking every topic row to the end of the transaction, in slug order:
    topic edits are serialized, and wait for settles in flight (see feedback.word.lock)."""
    rows = await (
        await conn.execute("SELECT slug, kind FROM topics ORDER BY slug FOR UPDATE")
    ).fetchall()
    return next((TopicKind(r["kind"]) for r in rows if r["slug"] == slug), None)


async def get(conn: psycopg.AsyncConnection, slug: str) -> Topic | None:
    row = await (await conn.execute("SELECT * FROM topics WHERE slug = %s", (slug,))).fetchone()
    return _topic(row) if row else None


async def kinds_at(
    conn: psycopg.AsyncConnection, revisions: dict[str, int]
) -> dict[str, TopicKind]:
    """Each topic's kind at the given revision: what it was when a run used it, whatever it
    has been changed to since."""
    rows = await (
        await conn.execute(
            "SELECT topic, revision, content->>'kind' AS kind FROM topic_versions "
            "WHERE topic = ANY(%s)",
            (list(revisions),),
        )
    ).fetchall()
    return {
        r["topic"]: TopicKind(r["kind"]) for r in rows if revisions[r["topic"]] == r["revision"]
    }
