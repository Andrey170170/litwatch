"""Carrying the owner's feedback from one v1 database into another (spec section 7): from the
database it was given in, before cutover, into the one that becomes production.

What carries: labels (not provisional ones, which nobody gave), reactions, appraisals, take
ratings with the takes they rate, Calibrate sessions and their answers, the Save toggle, and
reported misses. Only rows whose paper the target holds are copied, each with its original
time, so the newest word still wins between the two databases; running it again adds nothing.
A row the source no longer has (a cleared thumb, a removed label) is not carried. Topics are
not carried here: `litwatch topics export` and `import` do that.

Ids are local to a database, so what points at one is re-pointed or let go:

- a rated take is copied as history, bound to revision 0 of the paper and the topic, which no
  pair asks for: a shown pair whose newest take it becomes is owed a fresh one, which the next
  `litwatch write` (or nightly run) writes;
- a Calibrate item keeps its answer but not the verdict it was drawn at (that verdict exists
  only in the source); precision reads the answer and the group, not the verdict;
- a miss arrives without its trace, marked failed so Settings offers to trace it again.
"""

from __future__ import annotations

from typing import Any

import psycopg

from litwatch.feedback import word

# The newest migration whose tables are read here (Calibrate).
SOURCE_AT_LEAST = 9
NO_TRACE = "carried from another database without its trace; trace it again"


class SourceError(RuntimeError):
    """The source is not a v1 database this can read."""


async def _all(conn: psycopg.AsyncConnection, query: str, params: Any = ()) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def _check_source(source: psycopg.AsyncConnection) -> None:
    version = 0
    if (await _all(source, "SELECT to_regclass('schema_migrations') IS NOT NULL AS v1"))[0]["v1"]:
        version = (await _all(source, "SELECT max(version) AS n FROM schema_migrations"))[0]["n"]
    if (version or 0) < SOURCE_AT_LEAST:
        raise SourceError(
            f"the source is not a v1 database at migration {SOURCE_AT_LEAST} or later; "
            "run `litwatch db migrate` on it first"
        )


async def import_feedback(
    target: psycopg.AsyncConnection, source: psycopg.AsyncConnection
) -> dict[str, int]:
    """Copy the owner's feedback from `source` (only read) into `target`, then settle every
    paper it concerns without announcing anything. Returns what was added or replaced per
    kind, how many source rows had no paper here, and what the settle changed."""
    await _check_source(source)
    rows = {
        "labels": await _all(
            source,
            "SELECT paper_id, topic, value, grade, origin, note, labelled_at FROM labels "
            "WHERE NOT provisional ORDER BY paper_id, topic",
        ),
        "reactions": await _all(
            source,
            "SELECT paper_id, topic, value, reason, reacted_at FROM reactions ORDER BY paper_id",
        ),
        "appraisals": await _all(
            source, "SELECT paper_id, value, note, at FROM appraisals ORDER BY id"
        ),
        "take_ratings": await _all(
            source,
            "SELECT k.paper_id, k.topic, k.grade, k.source, k.text, k.written_at, r.accurate, "
            "r.at FROM take_ratings r JOIN takes k ON k.id = r.take_id ORDER BY k.id",
        ),
        "calibration_items": await _all(
            source,
            "SELECT s.seed, s.drawn_at, i.position, i.paper_id, i.topic, i.why, i.answer, "
            "i.answered_at FROM calibration_items i "
            "JOIN calibration_sessions s ON s.id = i.session_id ORDER BY s.id, i.position",
        ),
        "library": await _all(
            source,
            "SELECT paper_id, hand, hand_at FROM library WHERE hand IS NOT NULL ORDER BY paper_id",
        ),
        "misses": await _all(source, "SELECT paper_id, topic, reported_at FROM misses ORDER BY id"),
    }
    wanted = sorted({r["paper_id"] for found in rows.values() for r in found})
    held = {
        r["id"] for r in await _all(target, "SELECT id FROM papers WHERE id = ANY(%s)", (wanted,))
    }
    counts = {"no_paper": sum(r["paper_id"] not in held for f in rows.values() for r in f)}
    async with target.transaction():
        for kind, found in rows.items():
            counts[kind] = 0
            for row in (r for r in found if r["paper_id"] in held):
                counts[kind] += await COPY[kind](target, row)
    settled = {"papers": 0, "grades_changed": 0, "saved": 0, "unsaved": 0}
    for paper_id in sorted({r["paper_id"] for f in rows.values() for r in f} & held):
        result = await word.settle(target, paper_id, announce=False)
        settled["papers"] += 1
        settled["grades_changed"] += len(result.grades)
        if result.saved != result.was_saved:
            settled["saved" if result.saved else "unsaved"] += 1
    return counts | {f"settled_{k}": v for k, v in settled.items()}


async def _label(conn: psycopg.AsyncConnection, row: dict[str, Any]) -> int:
    """The newer label wins; a provisional one here always gives way, and then the paper takes
    the source's origin (how it was chosen there), as a label the owner gives does."""
    cur = await conn.execute(
        """
        INSERT INTO labels (paper_id, topic, value, grade, origin, provisional, note, labelled_at)
        VALUES (%(paper_id)s, %(topic)s, %(value)s, %(grade)s, %(origin)s, false, %(note)s,
                %(labelled_at)s)
        ON CONFLICT (paper_id, topic) DO UPDATE SET
            value = EXCLUDED.value, grade = EXCLUDED.grade, note = EXCLUDED.note,
            labelled_at = EXCLUDED.labelled_at, provisional = false,
            origin = CASE WHEN labels.provisional THEN EXCLUDED.origin ELSE labels.origin END
        WHERE labels.provisional OR EXCLUDED.labelled_at > labels.labelled_at
        """,
        row,
    )
    return cur.rowcount


async def _reaction(conn: psycopg.AsyncConnection, row: dict[str, Any]) -> int:
    cur = await conn.execute(
        """
        INSERT INTO reactions (paper_id, topic, value, reason, reacted_at)
        VALUES (%(paper_id)s, %(topic)s, %(value)s, %(reason)s, %(reacted_at)s)
        ON CONFLICT (paper_id, coalesce(topic, '')) DO UPDATE SET
            value = EXCLUDED.value, reason = EXCLUDED.reason, reacted_at = EXCLUDED.reacted_at
        WHERE EXCLUDED.reacted_at > reactions.reacted_at
        """,
        row,
    )
    return cur.rowcount


async def _appraisal(conn: psycopg.AsyncConnection, row: dict[str, Any]) -> int:
    """Appraisals are a history; one is the same as another given at the same moment."""
    cur = await conn.execute(
        """
        INSERT INTO appraisals (paper_id, value, note, at)
        SELECT %(paper_id)s, %(value)s, %(note)s, %(at)s
        WHERE NOT EXISTS (SELECT 1 FROM appraisals WHERE paper_id = %(paper_id)s AND at = %(at)s)
        """,
        row,
    )
    return cur.rowcount


async def _take_rating(conn: psycopg.AsyncConnection, row: dict[str, Any]) -> int:
    take = await (
        await conn.execute(
            "SELECT id FROM takes WHERE paper_id = %(paper_id)s AND topic = %(topic)s "
            "AND written_at = %(written_at)s AND text = %(text)s",
            row,
        )
    ).fetchone()
    if take is None:
        take = await (
            await conn.execute(
                """
                INSERT INTO takes (paper_id, topic, paper_revision, topic_revision, grade, source,
                                   text, written_at)
                VALUES (%(paper_id)s, %(topic)s, 0, 0, %(grade)s, %(source)s, %(text)s,
                        %(written_at)s)
                RETURNING id
                """,
                row,
            )
        ).fetchone()
    cur = await conn.execute(
        """
        INSERT INTO take_ratings (take_id, accurate, at) VALUES (%s, %s, %s)
        ON CONFLICT (take_id) DO UPDATE SET accurate = EXCLUDED.accurate, at = EXCLUDED.at
        WHERE EXCLUDED.at > take_ratings.at
        """,
        (take["id"], row["accurate"], row["at"]),
    )
    return cur.rowcount


async def _calibration_item(conn: psycopg.AsyncConnection, row: dict[str, Any]) -> int:
    """A session is known by its seed and when it was drawn; an item by its position, whose
    newer answer wins."""
    session = (
        await (
            await conn.execute(
                "SELECT id FROM calibration_sessions "
                "WHERE seed = %(seed)s AND drawn_at = %(drawn_at)s",
                row,
            )
        ).fetchone()
        or await (
            await conn.execute(
                "INSERT INTO calibration_sessions (seed, drawn_at) VALUES (%(seed)s, %(drawn_at)s) "
                "RETURNING id",
                row,
            )
        ).fetchone()
    )
    cur = await conn.execute(
        """
        INSERT INTO calibration_items (session_id, position, paper_id, topic, why, answer,
                                       answered_at)
        VALUES (%(session)s, %(position)s, %(paper_id)s, %(topic)s, %(why)s, %(answer)s,
                %(answered_at)s)
        ON CONFLICT (session_id, position) DO UPDATE SET
            answer = EXCLUDED.answer, answered_at = EXCLUDED.answered_at
        WHERE EXCLUDED.answered_at > coalesce(calibration_items.answered_at, '-infinity')
        """,
        row | {"session": session["id"]},
    )
    return cur.rowcount


async def _hand(conn: psycopg.AsyncConnection, row: dict[str, Any]) -> int:
    """The owner's newest use of the Save toggle, from either database."""
    cur = await conn.execute(
        """
        INSERT INTO library (paper_id, hand, hand_at) VALUES (%(paper_id)s, %(hand)s, %(hand_at)s)
        ON CONFLICT (paper_id) DO UPDATE SET hand = EXCLUDED.hand, hand_at = EXCLUDED.hand_at
        WHERE library.hand_at IS NULL OR EXCLUDED.hand_at > library.hand_at
        """,
        row,
    )
    return cur.rowcount


async def _miss(conn: psycopg.AsyncConnection, row: dict[str, Any]) -> int:
    cur = await conn.execute(
        """
        INSERT INTO misses (paper_id, topic, reported_at, trace, trace_error)
        VALUES (%(paper_id)s, %(topic)s, %(reported_at)s, 'failed', %(error)s)
        ON CONFLICT (paper_id, topic) DO NOTHING
        """,
        row | {"error": NO_TRACE},
    )
    return cur.rowcount


COPY = {
    "labels": _label,
    "reactions": _reaction,
    "appraisals": _appraisal,
    "take_ratings": _take_rating,
    "calibration_items": _calibration_item,
    "library": _hand,
    "misses": _miss,
}
