"""What the owner said about papers apart from labels (thumbs, the Save toggle), and what the
system keeps of its consequences: effective Fits and grades, rarity and the library."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Effective, Grade, Hand, Rarity, Reaction, Weight
from litwatch.core.grading import DEPTH_OFFSETS

# -- reactions -------------------------------------------------------------------------------------


def _reaction(row: dict[str, Any]) -> Reaction:
    return Reaction(**{k: row[k] for k in Reaction.model_fields})


async def set_reaction(conn: psycopg.AsyncConnection, reaction: Reaction) -> Reaction:
    """Store a thumb, replacing the one on the same paper and topic."""
    row = await (
        await conn.execute(
            """
            INSERT INTO reactions (paper_id, topic, value, reason) VALUES (%s, %s, %s, %s)
            ON CONFLICT (paper_id, coalesce(topic, '')) DO UPDATE SET
                value = EXCLUDED.value, reason = EXCLUDED.reason, reacted_at = clock_timestamp()
            RETURNING *
            """,
            (
                reaction.paper_id,
                reaction.topic,
                reaction.value.value,
                reaction.reason.value if reaction.reason else None,
            ),
        )
    ).fetchone()
    return _reaction(row)


async def clear_reaction(conn: psycopg.AsyncConnection, paper_id: str, topic: str | None) -> bool:
    cur = await conn.execute(
        "DELETE FROM reactions WHERE paper_id = %s AND coalesce(topic, '') = coalesce(%s, '')",
        (paper_id, topic),
    )
    return cur.rowcount > 0


async def reactions_for(conn: psycopg.AsyncConnection, paper_id: str) -> list[Reaction]:
    rows = await (
        await conn.execute("SELECT * FROM reactions WHERE paper_id = %s", (paper_id,))
    ).fetchall()
    return [_reaction(r) for r in rows]


# -- effective grades and rarity ------------------------------------------------------------------


async def effective_for(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Effective]:
    """The paper's stored effective Fit, grade and topic-page rarity per topic."""
    rows = await (
        await conn.execute(
            "SELECT topic, fit, grade, rarity FROM effective_grades WHERE paper_id = %s",
            (paper_id,),
        )
    ).fetchall()
    return {r["topic"]: Effective(fit=r["fit"], grade=r["grade"], rarity=r["rarity"]) for r in rows}


async def focus_grades(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Grade]:
    """The paper's effective grade per followed Focus topic."""
    rows = await (
        await conn.execute(
            "SELECT g.topic, g.grade FROM effective_grades g "
            "JOIN topics t ON t.class = g.topic AND t.depth = 'focus' WHERE g.paper_id = %s",
            (paper_id,),
        )
    ).fetchall()
    return {r["topic"]: Grade(r["grade"]) for r in rows}


async def keeps_pdf(conn: psycopg.AsyncConnection, paper_id: str) -> bool:
    """Whether the storage policy keeps the paper's PDF (archive.md): it is saved, or graded
    relevant or above on a followed topic. A paused topic is followed (`feedback.word.settle`
    keeps its grades): its papers keep their PDFs, so pausing and resuming changes none."""
    row = await (
        await conn.execute(
            """
            SELECT EXISTS (SELECT 1 FROM library WHERE paper_id = %(p)s AND saved)
                OR EXISTS (
                    SELECT 1 FROM effective_grades g
                    JOIN topics t ON t.class = g.topic AND t.status <> 'draft'
                    WHERE g.paper_id = %(p)s AND g.grade IN ('relevant', 'must_read')
                ) AS keeps
            """,
            {"p": paper_id},
        )
    ).fetchone()
    return row["keeps"]


async def set_effective(
    conn: psycopg.AsyncConnection, paper_id: str, topic: str, found: Effective | None
) -> None:
    if found is None:
        await conn.execute(
            "DELETE FROM effective_grades WHERE paper_id = %s AND topic = %s", (paper_id, topic)
        )
        return
    await conn.execute(
        """
        INSERT INTO effective_grades (paper_id, topic, fit, grade, rarity)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (paper_id, topic) DO UPDATE SET
            fit = EXCLUDED.fit, grade = EXCLUDED.grade, rarity = EXCLUDED.rarity,
            updated_at = now()
        """,
        (paper_id, topic, found.fit.value, found.grade.value, found.rarity.value),
    )


async def rarity_of(
    conn: psycopg.AsyncConnection, paper_id: str
) -> tuple[Rarity, str | None, Weight | None] | None:
    """The stored level, best topic and the Weight the level was set with."""
    row = await (
        await conn.execute(
            "SELECT level, best_topic, weight FROM rarities WHERE paper_id = %s", (paper_id,)
        )
    ).fetchone()
    if row is None:
        return None
    return Rarity(row["level"]), row["best_topic"], Weight(row["weight"]) if row["weight"] else None


async def tied(conn: psycopg.AsyncConnection) -> list[str]:
    """Papers whose highest level in a mixed list comes from two or more followed topics: the
    owner's order picks their best topic (`core.mixed_rarity`), so a new order can change it."""
    rows = await (
        await conn.execute(
            """
            SELECT paper_id FROM (
                SELECT paper_id, level, max(level) OVER (PARTITION BY paper_id) AS top FROM (
                    SELECT g.paper_id,
                           greatest(array_position(%(levels)s::text[], g.rarity) - 1
                                    - (%(offsets)s::jsonb ->> t.depth)::int, 0) AS level
                    FROM effective_grades g
                    JOIN topics t ON t.class = g.topic AND t.status <> 'draft'
                ) lowered
            ) leveled
            WHERE level = top GROUP BY paper_id HAVING count(*) > 1 ORDER BY paper_id
            """,
            {
                "levels": [r.value for r in Rarity],
                "offsets": Jsonb({d.value: n for d, n in DEPTH_OFFSETS.items()}),
            },
        )
    ).fetchall()
    return [r["paper_id"] for r in rows]


async def in_collection(conn: psycopg.AsyncConnection, paper_id: str) -> bool:
    """Whether the paper has been processed: classified, by the classifier or by hand, at some
    revision. A paper outside the collection has no rarity (pipeline.md, section 5)."""
    row = await (
        await conn.execute("SELECT 1 FROM classifications WHERE paper_id = %s LIMIT 1", (paper_id,))
    ).fetchone()
    return row is not None


async def set_rarity(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    level: Rarity,
    best_topic: str | None,
    weight: Weight | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO rarities (paper_id, level, best_topic, weight) VALUES (%s, %s, %s, %s)
        ON CONFLICT (paper_id) DO UPDATE SET
            level = EXCLUDED.level, best_topic = EXCLUDED.best_topic, weight = EXCLUDED.weight,
            updated_at = now()
        """,
        (paper_id, level.value, best_topic, weight.value if weight else None),
    )


# -- library ---------------------------------------------------------------------------------------


async def library_row(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    return await (
        await conn.execute("SELECT * FROM library WHERE paper_id = %s", (paper_id,))
    ).fetchone()


async def set_hand(conn: psycopg.AsyncConnection, paper_id: str, hand: Hand) -> None:
    await conn.execute(
        """
        INSERT INTO library (paper_id, hand, hand_at) VALUES (%s, %s, now())
        ON CONFLICT (paper_id) DO UPDATE SET hand = EXCLUDED.hand, hand_at = EXCLUDED.hand_at
        """,
        (paper_id, hand.value),
    )


async def set_saved(conn: psycopg.AsyncConnection, paper_id: str, saved: bool) -> None:
    await conn.execute(
        """
        INSERT INTO library (paper_id, saved, saved_at) VALUES (%s, %s, CASE WHEN %s THEN now() END)
        ON CONFLICT (paper_id) DO UPDATE SET
            saved = EXCLUDED.saved, saved_at = EXCLUDED.saved_at
        """,
        (paper_id, saved, saved),
    )


async def saved(conn: psycopg.AsyncConnection) -> list[tuple[str, datetime]]:
    """The library: paper ids with when each was saved, newest first."""
    rows = await (
        await conn.execute(
            "SELECT paper_id, saved_at FROM library WHERE saved ORDER BY saved_at DESC, paper_id"
        )
    ).fetchall()
    return [(r["paper_id"], r["saved_at"]) for r in rows]


async def touched(conn: psycopg.AsyncConnection) -> list[str]:
    """Every paper something could have an effective grade, a rarity or a library state for."""
    rows = await (
        await conn.execute(
            """
            SELECT paper_id FROM verdicts UNION SELECT paper_id FROM labels
            UNION SELECT paper_id FROM reactions UNION SELECT paper_id FROM library
            UNION SELECT paper_id FROM effective_grades UNION SELECT paper_id FROM classifications
            UNION SELECT paper_id FROM rarities ORDER BY 1
            """
        )
    ).fetchall()
    return [r["paper_id"] for r in rows]
