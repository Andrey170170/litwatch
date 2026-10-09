"""Topics and their revisions."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Status, Topic


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
    )


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
        await conn.execute(
            """
            INSERT INTO topics (slug, name, kind, subject, rubric, interests, keywords, status,
                                version, revision)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name, kind = EXCLUDED.kind, subject = EXCLUDED.subject,
                rubric = EXCLUDED.rubric, interests = EXCLUDED.interests,
                keywords = EXCLUDED.keywords, status = EXCLUDED.status,
                version = EXCLUDED.version, revision = EXCLUDED.revision, updated_at = now()
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
            ),
        )
        if changed:
            content = topic.model_dump(mode="json", exclude={"revision", "status", "name"})
            await conn.execute(
                "INSERT INTO topic_versions (topic, revision, hash, content) "
                "VALUES (%s, %s, %s, %s)",
                (topic.slug, revision, topic.version, Jsonb(content)),
            )
    return topic.model_copy(update={"revision": revision})


async def all(conn: psycopg.AsyncConnection, status: Status | None = Status.ACTIVE) -> list[Topic]:
    """Topics with the given status (every topic for None), Watch first, then by slug."""
    rows = await (
        await conn.execute(
            "SELECT * FROM topics WHERE %s::text IS NULL OR status = %s "
            "ORDER BY array_position(ARRAY['watch', 'radar', 'pulse'], kind), slug",
            (status.value if status else None, status.value if status else None),
        )
    ).fetchall()
    return [_topic(r) for r in rows]


async def get(conn: psycopg.AsyncConnection, slug: str) -> Topic | None:
    row = await (await conn.execute("SELECT * FROM topics WHERE slug = %s", (slug,))).fetchone()
    return _topic(row) if row else None
