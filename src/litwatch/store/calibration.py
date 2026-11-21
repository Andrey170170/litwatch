"""Calibrate's sessions (`calibration_sessions`, `calibration_items`), the owner's appraisals,
and the reads behind Calibrate's pool, its sessions and its precision (feedback.calibrate)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from litwatch.gates.database import ReadUnit, WriteUnit
from litwatch.store.analyses import CURRENT_WEIGHT


async def _all(unit: ReadUnit, query: str, params: Any = ()) -> list[dict]:
    return await (await unit.execute(query, params)).fetchall()


async def pool(unit: ReadUnit, topic: str, days: int) -> list[dict[str, Any]]:
    """The topic's current verdicts of the trailing `days`, made on the paper's current text and
    with no label, each with its Fit and the paper's current Weight."""
    return await _all(
        unit,
        f"""
        SELECT paper_id, id AS verdict_id, fit,
               (SELECT w.weight FROM papers p JOIN LATERAL ({CURRENT_WEIGHT}) w ON true
                WHERE p.id = v.paper_id) AS weight
        FROM (
            SELECT DISTINCT ON (paper_id) * FROM verdicts WHERE topic = %s
            ORDER BY paper_id, paper_revision DESC, recorded_at DESC, id DESC
        ) v
        -- On the paper's current text, so its Fit and Weight are judgements of the same text.
        WHERE paper_revision = (SELECT revision FROM papers WHERE id = v.paper_id)
          AND recorded_at >= now() - make_interval(days => %s)
          AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.paper_id = v.paper_id
                          AND l.topic = v.topic)
        ORDER BY paper_id
        """,
        (topic, days),
    )


async def new_session(
    unit: WriteUnit, seed: int, items: Sequence[tuple[str, str, str, int | None]]
) -> int:
    """A session drawn with `seed`, its `items` (paper, topic, why, verdict) in order. Returns
    its id."""
    session = await (
        await unit.execute(
            "INSERT INTO calibration_sessions (seed) VALUES (%s) RETURNING id", (seed,)
        )
    ).fetchone()
    for position, item in enumerate(items, 1):
        await unit.execute(
            "INSERT INTO calibration_items "
            "(session_id, position, paper_id, topic, why, verdict_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (session["id"], position, *item),
        )
    return session["id"]


async def waiting(unit: ReadUnit) -> list[dict[str, Any]]:
    """The papers waiting for a word on a Focus topic, with no label there: last night's
    splits that straddle the line, reported misses, and papers with a word-carrying thumb."""
    return await _all(
        unit,
        """
        SELECT s.paper_id, s.topic, s.why, s.verdict_id FROM (
            SELECT DISTINCT ON (v.paper_id, v.topic) v.paper_id, v.topic, 'split' AS why,
                   v.id AS verdict_id, v.opinions->'judge'->>'fit' AS a,
                   v.opinions->'check'->>'fit' AS b, v.recorded_at
            FROM verdicts v JOIN topics t ON t.class = v.topic AND t.depth = 'focus'
            ORDER BY v.paper_id, v.topic, v.paper_revision DESC, v.recorded_at DESC,
                     v.id DESC) s
        -- the two Fits on either side of related: one grader would have kept it, one not
        WHERE (a IN ('central', 'related')) <> (b IN ('central', 'related'))
          -- last night's: recorded since the latest nightly run started
          AND s.recorded_at >= coalesce(
              (SELECT started_at FROM runs WHERE kind = 'nightly' ORDER BY id DESC LIMIT 1),
              now() - interval '36 hours')
          AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.paper_id = s.paper_id
                          AND l.topic = s.topic)
        UNION ALL
        -- a miss or a reaction keeps the verdict standing when it joins, as a draw does
        SELECT m.paper_id, m.topic, 'miss', (SELECT v.id FROM verdicts v
            WHERE v.paper_id = m.paper_id AND v.topic = m.topic
            ORDER BY v.recorded_at DESC, v.id DESC LIMIT 1) FROM misses m
        JOIN topics t ON t.class = m.topic AND t.depth = 'focus'
        WHERE NOT EXISTS (SELECT 1 FROM labels l WHERE l.paper_id = m.paper_id
                          AND l.topic = m.topic)
        UNION ALL
        SELECT r.paper_id, r.topic, 'reaction', (SELECT v.id FROM verdicts v
            WHERE v.paper_id = r.paper_id AND v.topic = r.topic
            ORDER BY v.recorded_at DESC, v.id DESC LIMIT 1) FROM reactions r
        JOIN topics t ON t.class = r.topic AND t.depth = 'focus'
        WHERE r.reason IN ('wrong_topic', 'great_find')
          AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.paper_id = r.paper_id
                          AND l.topic = r.topic)
        """,
    )


async def add(unit: WriteUnit, session_id: int, rows: Sequence[dict[str, Any]]) -> int:
    """Add `rows` (`waiting`'s) after the session's last item, each paper and topic once: one
    the session holds already is left out. Returns how many were added."""
    last = await (
        await unit.execute(
            "SELECT coalesce(max(position), 0) AS n FROM calibration_items WHERE session_id = %s",
            (session_id,),
        )
    ).fetchone()
    position, added = last["n"], 0
    for row in rows:
        cur = await unit.execute(
            "INSERT INTO calibration_items (session_id, position, paper_id, topic, why, "
            "verdict_id) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                session_id,
                position + 1,
                row["paper_id"],
                row["topic"],
                row["why"],
                row["verdict_id"],
            ),
        )
        if cur.rowcount:
            position, added = position + 1, added + 1
    return added


async def item(unit: ReadUnit, session_id: int, position: int) -> dict | None:
    return await (
        await unit.execute(
            "SELECT * FROM calibration_items WHERE session_id = %s AND position = %s",
            (session_id, position),
        )
    ).fetchone()


async def answered(unit: WriteUnit, session_id: int, position: int, answer: str | None) -> None:
    """The item is answered now; `answer`, its drawn topic's Fit, replaces the one recorded
    unless it is None."""
    await unit.execute(
        "UPDATE calibration_items SET answer = coalesce(%s, answer), answered_at = now() "
        "WHERE session_id = %s AND position = %s",
        (answer, session_id, position),
    )


async def label_values(unit: ReadUnit, paper_id: str) -> dict[str, str]:
    """The paper's labels' values, by topic."""
    rows = await _all(
        unit,
        "SELECT topic, value FROM labels WHERE paper_id = %s",
        (paper_id,),
    )
    return {r["topic"]: r["value"] for r in rows}


async def appraisal(unit: ReadUnit, paper_id: str) -> tuple[str | None, str | None]:
    """The paper's newest appraisal, its value and note, or (None, None) without one. The newest
    is the last given (`at`): an imported one keeps its time, not its id."""
    row = await (
        await unit.execute(
            "SELECT value, note FROM appraisals WHERE paper_id = %s "
            "ORDER BY at DESC, id DESC LIMIT 1",
            (paper_id,),
        )
    ).fetchone()
    return (row["value"], row["note"]) if row else (None, None)


async def appraise(unit: WriteUnit, paper_id: str, value: str | None, note: str | None) -> None:
    """A new appraisal of the paper; `value` and `note` None clear it."""
    await unit.execute(
        "INSERT INTO appraisals (paper_id, value, note) VALUES (%s, %s, %s)",
        (paper_id, value, note),
    )


# The pipeline's Fit on a session item's drawn topic: the verdict the draw read, else the current.
_VERDICT = """coalesce((SELECT v.fit FROM verdicts v WHERE v.id = i.verdict_id),
                 (SELECT v.fit FROM verdicts v WHERE v.paper_id = i.paper_id
                  AND v.topic = i.topic ORDER BY v.recorded_at DESC, v.id DESC LIMIT 1))"""

# Whether the owner's answer differs from the pipeline's Fit: another Fit than its verdict's,
# or, while the verdict is gone, the other side from the draw's. Unsure never differs.
DIFFERS = f"""i.answer IS NOT NULL AND i.answer <> 'unsure' AND CASE
    WHEN ({_VERDICT}) IS NOT NULL THEN i.answer <> ({_VERDICT})
    WHEN i.why = 'above' THEN i.answer IN ('adjacent', 'none')
    WHEN i.why = 'below' THEN i.answer IN ('central', 'related')
    ELSE false END"""


async def sessions(unit: ReadUnit, limit: int = 20) -> list[dict[str, Any]]:
    """The newest sessions: how many papers each drew, how many are answered, and how many
    answers differ from the pipeline's Fit (`DIFFERS`)."""
    return await _all(
        unit,
        f"""
        SELECT s.id, s.seed, s.drawn_at, count(i.*) AS papers, count(i.answered_at) AS answered,
               count(*) FILTER (WHERE {DIFFERS}) AS differ
        FROM calibration_sessions s LEFT JOIN calibration_items i ON i.session_id = s.id
        GROUP BY s.id ORDER BY s.id DESC LIMIT %s
        """,
        (limit,),
    )


async def last_drawn(unit: ReadUnit) -> datetime | None:
    """When the newest session was drawn; None before the first."""
    row = await (
        await unit.execute("SELECT drawn_at FROM calibration_sessions ORDER BY id DESC LIMIT 1")
    ).fetchone()
    return row["drawn_at"] if row else None


async def session(unit: ReadUnit, session_id: int) -> dict[str, Any] | None:
    """A session's papers in order, each with the pipeline's Fit on the drawn topic (the verdict
    the draw read, else the current one), the answer recorded for it (`answer`, null until
    given) and what the owner has said since (`fit`, every current label; `appraisal`)."""
    found = await (
        await unit.execute("SELECT * FROM calibration_sessions WHERE id = %s", (session_id,))
    ).fetchone()
    if found is None:
        return None
    items = await _all(
        unit,
        f"""
        SELECT i.position, i.paper_id, i.topic, i.why, i.answer, i.answered_at, p.title,
               p.abstract, p.authors, p.published, {_VERDICT} AS verdict,
               (SELECT coalesce(jsonb_object_agg(l.topic, l.value), '{{}}') FROM labels l
                WHERE l.paper_id = i.paper_id) AS fit,
               (SELECT row_to_json(a) FROM (SELECT value, note FROM appraisals
                WHERE paper_id = i.paper_id ORDER BY at DESC, id DESC LIMIT 1) a) AS appraisal
        FROM calibration_items i JOIN papers p ON p.id = i.paper_id
        WHERE i.session_id = %s ORDER BY i.position
        """,
        (session_id,),
    )
    return found | {"items": items}


async def drawn(unit: ReadUnit, days: int) -> list[dict[str, Any]]:
    """Per Focus topic and side of the line (`why`, above or below) over the draws of the
    trailing `days`: how many were answered other than unsure, and how many central or
    related."""
    return await _all(
        unit,
        """
        SELECT t.class AS topic, i.why,
               count(*) FILTER (WHERE i.answer IS NOT NULL AND i.answer <> 'unsure') AS answered,
               count(*) FILTER (WHERE i.answer IN ('central', 'related')) AS relevant
        FROM topics t
        JOIN calibration_items i ON i.topic = t.class AND i.why IN ('above', 'below')
        JOIN calibration_sessions s ON s.id = i.session_id
             AND s.drawn_at >= now() - make_interval(days => %s)
        WHERE t.depth = 'focus'
        GROUP BY t.class, t.position, i.why ORDER BY t.position, i.why
        """,
        (days,),
    )


async def matters_no_topic(unit: ReadUnit) -> list[dict[str, Any]]:
    """Papers whose newest appraisal is "matters" and that are below relevant for every
    Focus topic, earlier words included: a missing topic or a narrow rubric."""
    return await _all(
        unit,
        """
        SELECT a.paper_id, a.note, a.at, p.title FROM (
            SELECT DISTINCT ON (paper_id) * FROM appraisals
            ORDER BY paper_id, at DESC, id DESC) a
        JOIN papers p ON p.id = a.paper_id
        WHERE a.value = 'matters'
          AND NOT EXISTS (SELECT 1 FROM effective_grades g JOIN topics t ON t.class = g.topic
                          WHERE g.paper_id = a.paper_id AND t.depth = 'focus'
                            AND g.grade IN ('must_read', 'relevant'))
        ORDER BY a.at DESC
        """,
    )
