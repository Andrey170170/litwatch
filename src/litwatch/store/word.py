"""What the owner said about papers apart from labels (thumbs, the Save toggle), and what the
system keeps of its consequences: effective Fits and grades, rarity and the library."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from litwatch.core import Effective, Grade, Hand, Rarity, Reaction, Weight
from litwatch.core.grading import DEPTH_OFFSETS
from litwatch.gates.database import ReadUnit, WriteUnit

# -- reactions -------------------------------------------------------------------------------------


def _reaction(row: dict[str, Any]) -> Reaction:
    return Reaction(**{k: row[k] for k in Reaction.model_fields})


async def set_reaction(unit: WriteUnit, reaction: Reaction) -> Reaction:
    """Store a thumb, replacing the one on the same paper and topic."""
    row = await (
        await unit.execute(
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


async def clear_reaction(unit: WriteUnit, paper_id: str, topic: str | None) -> bool:
    cur = await unit.execute(
        "DELETE FROM reactions WHERE paper_id = %s AND coalesce(topic, '') = coalesce(%s, '')",
        (paper_id, topic),
    )
    return cur.rowcount > 0


async def reactions_for(unit: ReadUnit, paper_id: str) -> list[Reaction]:
    rows = await (
        await unit.execute("SELECT * FROM reactions WHERE paper_id = %s", (paper_id,))
    ).fetchall()
    return [_reaction(r) for r in rows]


async def reactions_of(unit: ReadUnit, paper_ids: list[str]) -> dict[str, list[Reaction]]:
    """The thumbs on each of the papers that has any, by paper."""
    rows = await (
        await unit.execute("SELECT * FROM reactions WHERE paper_id = ANY(%s)", (paper_ids,))
    ).fetchall()
    found: dict[str, list[Reaction]] = {}
    for r in rows:
        found.setdefault(r["paper_id"], []).append(_reaction(r))
    return found


# -- effective grades and rarity ------------------------------------------------------------------


async def effective_for(unit: ReadUnit, paper_id: str) -> dict[str, Effective]:
    """The paper's stored effective Fit, grade and topic-page rarity per topic."""
    rows = await (
        await unit.execute(
            "SELECT topic, fit, grade, rarity FROM effective_grades WHERE paper_id = %s",
            (paper_id,),
        )
    ).fetchall()
    return {r["topic"]: Effective(fit=r["fit"], grade=r["grade"], rarity=r["rarity"]) for r in rows}


async def effective_of(unit: ReadUnit, paper_ids: list[str]) -> dict[str, dict[str, Effective]]:
    """`effective_for` of each of the papers that has a stored grade, by paper."""
    rows = await (
        await unit.execute(
            "SELECT paper_id, topic, fit, grade, rarity FROM effective_grades "
            "WHERE paper_id = ANY(%s)",
            (paper_ids,),
        )
    ).fetchall()
    found: dict[str, dict[str, Effective]] = {}
    for r in rows:
        found.setdefault(r["paper_id"], {})[r["topic"]] = Effective(
            fit=r["fit"], grade=r["grade"], rarity=r["rarity"]
        )
    return found


async def focus_grades(unit: ReadUnit, paper_id: str) -> dict[str, Grade]:
    """The paper's effective grade per followed Focus topic."""
    rows = await (
        await unit.execute(
            "SELECT g.topic, g.grade FROM effective_grades g "
            "JOIN topics t ON t.class = g.topic AND t.depth = 'focus' WHERE g.paper_id = %s",
            (paper_id,),
        )
    ).fetchall()
    return {r["topic"]: Grade(r["grade"]) for r in rows}


async def keeps_pdf(unit: ReadUnit, paper_id: str) -> bool:
    """Whether the storage policy keeps the paper's PDF (archive.md): it is saved, or graded
    relevant or above on a followed topic. A paused topic is followed (a settle,
    `feedback.word.settle_many`, keeps its grades): its papers keep their PDFs, so pausing and
    resuming changes none."""
    row = await (
        await unit.execute(
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
    unit: WriteUnit, paper_id: str, topic: str, found: Effective | None
) -> None:
    if found is None:
        await unit.execute(
            "DELETE FROM effective_grades WHERE paper_id = %s AND topic = %s", (paper_id, topic)
        )
        return
    await unit.execute(
        """
        INSERT INTO effective_grades (paper_id, topic, fit, grade, rarity)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (paper_id, topic) DO UPDATE SET
            fit = EXCLUDED.fit, grade = EXCLUDED.grade, rarity = EXCLUDED.rarity,
            updated_at = now()
        """,
        (paper_id, topic, found.fit.value, found.grade.value, found.rarity.value),
    )


async def rarities_of(
    unit: ReadUnit, paper_ids: list[str]
) -> dict[str, tuple[Rarity, str | None, Weight | None]]:
    """The stored level, best topic and the Weight the level was set with, of each of the
    papers that has a rarity, by paper."""
    rows = await (
        await unit.execute(
            "SELECT paper_id, level, best_topic, weight FROM rarities WHERE paper_id = ANY(%s)",
            (paper_ids,),
        )
    ).fetchall()
    return {
        r["paper_id"]: (
            Rarity(r["level"]),
            r["best_topic"],
            Weight(r["weight"]) if r["weight"] else None,
        )
        for r in rows
    }


async def topic_papers(unit: ReadUnit, slugs: list[str]) -> list[str]:
    """The papers any of the topics has a grade, verdict, label, reaction or grading failure
    for, each once, in id order."""
    rows = await (
        await unit.execute(
            "SELECT paper_id FROM effective_grades WHERE topic = ANY(%(t)s) "
            "UNION SELECT paper_id FROM verdicts WHERE topic = ANY(%(t)s) "
            "UNION SELECT paper_id FROM labels WHERE topic = ANY(%(t)s) "
            "UNION SELECT paper_id FROM reactions WHERE topic = ANY(%(t)s) "
            "UNION SELECT paper_id FROM step_failures "
            "WHERE step = 'grading' AND topic = ANY(%(t)s) "
            "ORDER BY 1",
            {"t": slugs},
        )
    ).fetchall()
    return [r["paper_id"] for r in rows]


async def tied(unit: ReadUnit) -> list[str]:
    """Papers whose highest level in a mixed list comes from two or more followed topics: the
    owner's order picks their best topic (`core.mixed_rarity`), so a new order can change it."""
    rows = await (
        await unit.execute(
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


async def in_collection(unit: ReadUnit, paper_id: str) -> bool:
    """Whether the paper has been processed: classified, by the classifier or by hand, at some
    revision. A paper outside the collection has no rarity (pipeline.md, section 5)."""
    row = await (
        await unit.execute("SELECT 1 FROM classifications WHERE paper_id = %s LIMIT 1", (paper_id,))
    ).fetchone()
    return row is not None


async def collected(unit: ReadUnit, paper_ids: list[str]) -> set[str]:
    """Those of the papers that are in the collection (`in_collection`)."""
    rows = await (
        await unit.execute(
            "SELECT DISTINCT paper_id FROM classifications WHERE paper_id = ANY(%s)",
            (paper_ids,),
        )
    ).fetchall()
    return {r["paper_id"] for r in rows}


async def set_rarity(
    unit: WriteUnit,
    paper_id: str,
    level: Rarity,
    best_topic: str | None,
    weight: Weight | None,
) -> None:
    await unit.execute(
        """
        INSERT INTO rarities (paper_id, level, best_topic, weight) VALUES (%s, %s, %s, %s)
        ON CONFLICT (paper_id) DO UPDATE SET
            level = EXCLUDED.level, best_topic = EXCLUDED.best_topic, weight = EXCLUDED.weight,
            updated_at = now()
        """,
        (paper_id, level.value, best_topic, weight.value if weight else None),
    )


# -- library ---------------------------------------------------------------------------------------


async def library_row(unit: ReadUnit, paper_id: str) -> dict[str, Any] | None:
    return await (
        await unit.execute("SELECT * FROM library WHERE paper_id = %s", (paper_id,))
    ).fetchone()


async def library_rows(unit: ReadUnit, paper_ids: list[str]) -> dict[str, dict[str, Any]]:
    """`library_row` of each of the papers that has one, by paper."""
    rows = await (
        await unit.execute("SELECT * FROM library WHERE paper_id = ANY(%s)", (paper_ids,))
    ).fetchall()
    return {r["paper_id"]: r for r in rows}


async def set_hand(unit: WriteUnit, paper_id: str, hand: Hand) -> None:
    await unit.execute(
        """
        INSERT INTO library (paper_id, hand, hand_at) VALUES (%s, %s, now())
        ON CONFLICT (paper_id) DO UPDATE SET hand = EXCLUDED.hand, hand_at = EXCLUDED.hand_at
        """,
        (paper_id, hand.value),
    )


async def set_saved(unit: WriteUnit, paper_id: str, saved: bool) -> None:
    await unit.execute(
        """
        INSERT INTO library (paper_id, saved, saved_at) VALUES (%s, %s, CASE WHEN %s THEN now() END)
        ON CONFLICT (paper_id) DO UPDATE SET
            saved = EXCLUDED.saved, saved_at = EXCLUDED.saved_at
        """,
        (paper_id, saved, saved),
    )


async def saved(unit: ReadUnit) -> list[tuple[str, datetime]]:
    """The library: paper ids with when each was saved, newest first."""
    rows = await (
        await unit.execute(
            "SELECT paper_id, saved_at FROM library WHERE saved ORDER BY saved_at DESC, paper_id"
        )
    ).fetchall()
    return [(r["paper_id"], r["saved_at"]) for r in rows]


async def touched(unit: ReadUnit) -> list[str]:
    """Every paper something could have an effective grade, a rarity or a library state for."""
    rows = await (
        await unit.execute(
            """
            SELECT paper_id FROM verdicts UNION SELECT paper_id FROM labels
            UNION SELECT paper_id FROM reactions UNION SELECT paper_id FROM library
            UNION SELECT paper_id FROM effective_grades UNION SELECT paper_id FROM classifications
            UNION SELECT paper_id FROM rarities ORDER BY 1
            """
        )
    ).fetchall()
    return [r["paper_id"] for r in rows]
