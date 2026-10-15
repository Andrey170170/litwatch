"""Calibrate: the one place that asks (spec §8 M3, decision 20).

A session holds about twenty papers, split evenly across the Watch topics and, within each,
half drawn at random from the papers graded relevant or above in the trailing 30 days and
half from those graded below, all without a label for that topic. On request it continues
with papers waiting for a word: last night's splits and papers carrying a word-carrying
thumb. Each answer is stored as it is given.
"""

from __future__ import annotations

import random
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, model_validator

from litwatch.core import Grade, Label, LabelOrigin, LabelValue, TopicKind
from litwatch.feedback import word
from litwatch.store import topics as topics_store

SESSION = 20
DRAW_DAYS = 30
PRECISION_DAYS = 90
SHOWN = ("must_read", "relevant")


async def _all(conn: psycopg.AsyncConnection, query: str, params: Any = ()) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def _pool(conn: psycopg.AsyncConnection, topic: str, above: bool) -> list[dict[str, Any]]:
    """The current verdicts of the trailing days on one side of the line, with no firm label.
    A verdict migrated from 0.14 is not v1's grading, so precision never judges one."""
    return await _all(
        conn,
        f"""
        SELECT paper_id, id AS verdict_id FROM (
            SELECT DISTINCT ON (paper_id) * FROM verdicts WHERE topic = %s
            ORDER BY paper_id, paper_revision DESC, topic_revision DESC, recorded_at DESC, id DESC
        ) v
        WHERE recorded_at >= now() - make_interval(days => %s) AND NOT migrated
          AND grade {"IN" if above else "NOT IN"} ('must_read', 'relevant')
          AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.paper_id = v.paper_id
                          AND l.topic = v.topic AND NOT l.provisional)
        ORDER BY paper_id
        """,
        (topic, DRAW_DAYS),
    )


async def draw(conn: psycopg.AsyncConnection, seed: int, size: int = SESSION) -> int:
    """Draw a session and return its id. The seed is recorded, so the draw can be repeated."""
    watched = [t.slug for t in await topics_store.all(conn) if t.kind == TopicKind.WATCH]
    per_group = max(1, size // (2 * max(1, len(watched))))
    rng = random.Random(seed)
    picked: list[tuple[str, str, str, int]] = []
    seen: set[str] = set()
    for topic in watched:
        for above in (True, False):
            pool = [r for r in await _pool(conn, topic, above) if r["paper_id"] not in seen]
            for row in rng.sample(pool, min(per_group, len(pool))):
                seen.add(row["paper_id"])
                picked.append(
                    (row["paper_id"], topic, "above" if above else "below", row["verdict_id"])
                )
    async with conn.transaction():
        session = await (
            await conn.execute(
                "INSERT INTO calibration_sessions (seed) VALUES (%s) RETURNING id", (seed,)
            )
        ).fetchone()
        for position, item in enumerate(picked, 1):
            await _add(conn, session["id"], position, *item)
    return session["id"]


async def _add(
    conn: psycopg.AsyncConnection,
    session_id: int,
    position: int,
    paper_id: str,
    topic: str,
    why: str,
    verdict_id: int | None,
) -> None:
    await conn.execute(
        "INSERT INTO calibration_items (session_id, position, paper_id, topic, why, verdict_id) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (session_id, position, paper_id, topic, why, verdict_id),
    )


async def more(conn: psycopg.AsyncConnection, session_id: int) -> int:
    """Add the papers waiting for a word that are not in the session yet: splits that
    straddle the line, reported misses not yet marked, and papers with a word-carrying thumb
    but no label. Returns how many."""
    rows = await _all(
        conn,
        """
        SELECT s.paper_id, s.topic, s.why, s.verdict_id, s.a, s.b FROM (
            SELECT DISTINCT ON (v.paper_id, v.topic) v.paper_id, v.topic, 'split' AS why,
                   v.id AS verdict_id, v.judge->>'grade' AS a, v.check_opinion->>'grade' AS b,
                   v.recorded_at
            FROM verdicts v JOIN topics t ON t.slug = v.topic AND t.kind = 'watch'
            ORDER BY v.paper_id, v.topic, v.paper_revision DESC, v.topic_revision DESC,
                     v.recorded_at DESC, v.id DESC) s
        WHERE (a IN ('must_read', 'relevant')) <> (b IN ('must_read', 'relevant'))
          -- last night's: recorded since the latest nightly run started
          AND s.recorded_at >= coalesce(
              (SELECT started_at FROM runs WHERE kind = 'nightly' ORDER BY id DESC LIMIT 1),
              now() - interval '36 hours')
          AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.paper_id = s.paper_id
                          AND l.topic = s.topic AND NOT l.provisional)
        UNION ALL
        SELECT m.paper_id, m.topic, 'miss', NULL, NULL, NULL FROM misses m
        WHERE NOT EXISTS (SELECT 1 FROM labels l WHERE l.paper_id = m.paper_id
                          AND l.topic = m.topic AND NOT l.provisional)
        UNION ALL
        SELECT r.paper_id, r.topic, 'reaction', NULL, NULL, NULL FROM reactions r
        JOIN topics t ON t.slug = r.topic AND t.kind = 'watch'
        WHERE r.reason IN ('unrelated', 'great_find')
          AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.paper_id = r.paper_id
                          AND l.topic = r.topic AND NOT l.provisional)
        """,
    )
    async with conn.transaction():
        last = await (
            await conn.execute(
                "SELECT coalesce(max(position), 0) AS n FROM calibration_items "
                "WHERE session_id = %s",
                (session_id,),
            )
        ).fetchone()
        position, added = last["n"], 0
        for row in rows:
            cur = await conn.execute(
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


class Fit(BaseModel):
    value: LabelValue
    grade: Grade | None = None  # with relevant: must read or worth reading

    @model_validator(mode="after")
    def _grade_fits_value(self) -> Fit:
        if self.value == LabelValue.RELEVANT:
            if self.grade is None or not self.grade.at_least(Grade.RELEVANT):
                raise ValueError("relevant needs a grade: must_read or relevant")
        elif self.grade is not None:
            raise ValueError("a grade goes only with relevant")
        return self


class Answer(BaseModel):
    """One paper's answers. Fit for the topic it was drawn or taken for is required."""

    fit: dict[str, Fit]
    value: Literal["matters", "fine", "not_for_me"] | None = None
    note: str | None = None


async def answer(
    conn: psycopg.AsyncConnection, session_id: int, position: int, given: Answer
) -> None:
    item = await (
        await conn.execute(
            "SELECT * FROM calibration_items WHERE session_id = %s AND position = %s",
            (session_id, position),
        )
    ).fetchone()
    if item is None:
        raise LookupError("no such item")
    if item["topic"] not in given.fit:
        raise ValueError(f"fit for {item['topic']} is required")
    watched = {t.slug for t in await topics_store.all(conn, None) if t.kind == TopicKind.WATCH}
    if unknown := sorted(set(given.fit) - watched):
        raise ValueError(f"not Watch topics: {', '.join(unknown)}")
    async with conn.transaction():
        for topic, fit in given.fit.items():
            # The drawn topic's answer is the sample; any other is the owner's own choice.
            drawn = topic == item["topic"] and item["why"] in ("above", "below")
            await word.label(
                conn,
                Label(
                    paper_id=item["paper_id"],
                    topic=topic,
                    value=fit.value,
                    grade=fit.grade if fit.value == LabelValue.RELEVANT else None,
                    origin=LabelOrigin.SAMPLE if drawn else LabelOrigin.READER,
                ),
            )
        # An appraisal changed, cleared included, is a new row; the same one sent again is not.
        # The newest is the last given (`at`): an imported one keeps its time, not its id.
        newest = await (
            await conn.execute(
                "SELECT value, note FROM appraisals WHERE paper_id = %s "
                "ORDER BY at DESC, id DESC LIMIT 1",
                (item["paper_id"],),
            )
        ).fetchone()
        said = (given.value, given.note or None)
        if said != ((newest["value"], newest["note"]) if newest else (None, None)):
            await conn.execute(
                "INSERT INTO appraisals (paper_id, value, note) VALUES (%s, %s, %s)",
                (item["paper_id"], *said),
            )
        await conn.execute(
            "UPDATE calibration_items SET answer = %s, answered_at = now() "
            "WHERE session_id = %s AND position = %s",
            (given.fit[item["topic"]].value.value, session_id, position),
        )


async def session(conn: psycopg.AsyncConnection, session_id: int) -> dict[str, Any] | None:
    """A session's papers in order, each with what the owner has said."""
    found = await (
        await conn.execute("SELECT * FROM calibration_sessions WHERE id = %s", (session_id,))
    ).fetchone()
    if found is None:
        return None
    items = await _all(
        conn,
        """
        SELECT i.position, i.paper_id, i.topic, i.why, i.answered_at, p.title, p.abstract,
               p.authors, p.published,
               (SELECT coalesce(jsonb_object_agg(l.topic, jsonb_build_object('value', l.value,
                    'grade', l.grade)), '{}') FROM labels l
                WHERE l.paper_id = i.paper_id AND NOT l.provisional) AS fit,
               (SELECT row_to_json(a) FROM (SELECT value, note FROM appraisals
                WHERE paper_id = i.paper_id ORDER BY at DESC, id DESC LIMIT 1) a) AS appraisal
        FROM calibration_items i JOIN papers p ON p.id = i.paper_id
        WHERE i.session_id = %s ORDER BY i.position
        """,
        (session_id,),
    )
    return found | {"items": items}


async def precision(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    """Per Watch topic over the trailing 90 days of draws: precision at relevant (the drawn
    upper group labelled relevant) and how often grading dropped a wanted paper (the lower
    group labelled relevant). Unsure and unanswered draws count nowhere."""
    rows = await _all(
        conn,
        """
        SELECT t.slug AS topic, i.why,
               count(*) FILTER (WHERE i.answer IS NOT NULL AND i.answer <> 'unsure') AS answered,
               count(*) FILTER (WHERE i.answer = 'relevant') AS relevant
        FROM topics t
        JOIN calibration_items i ON i.topic = t.slug AND i.why IN ('above', 'below')
        JOIN calibration_sessions s ON s.id = i.session_id
             AND s.drawn_at >= now() - make_interval(days => %s)
        WHERE t.kind = 'watch'
        GROUP BY t.slug, t.position, i.why ORDER BY t.position, i.why
        """,
        (PRECISION_DAYS,),
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = out.setdefault(r["topic"], {"topic": r["topic"]})
        share = r["relevant"] / r["answered"] if r["answered"] else None
        key = "precision" if r["why"] == "above" else "dropped"
        entry[key] = share
        entry[f"{key}_n"] = r["answered"]
    return list(out.values())


async def matters_no_topic(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    """Papers whose newest appraisal is "matters" and that are below relevant for every
    Watch topic, earlier words included: a missing topic or a narrow rubric."""
    return await _all(
        conn,
        """
        SELECT a.paper_id, a.note, a.at, p.title FROM (
            SELECT DISTINCT ON (paper_id) * FROM appraisals
            ORDER BY paper_id, at DESC, id DESC) a
        JOIN papers p ON p.id = a.paper_id
        WHERE a.value = 'matters'
          AND NOT EXISTS (SELECT 1 FROM effective_grades g JOIN topics t ON t.slug = g.topic
                          WHERE g.paper_id = a.paper_id AND t.kind = 'watch'
                            AND g.grade IN ('must_read', 'relevant'))
        ORDER BY a.at DESC
        """,
    )
