"""The queries behind the reading side: the status line, issues, a paper's page, the papers
table and the topic cards. Read-only; every grade here is the effective one."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg import sql

GRADE_ORDER = "ARRAY['must_read', 'relevant', 'adjacent', 'none']"
SHOWN = ("must_read", "relevant")
STALE_AFTER = timedelta(hours=36)
# A take whose pair is owed a new one describes text, a rubric or a length that no longer
# holds; current views show no take until the writer catches up. Issues keep theirs.
FRESH = (
    "AND NOT EXISTS (SELECT 1 FROM due d WHERE d.paper_id = k.paper_id "
    "AND d.topic = k.topic AND d.for_take)"
)
# A stored JSON null in `authors` reads as no authors.
AUTHORS = "CASE WHEN jsonb_typeof(p.authors) = 'array' THEN p.authors ELSE '[]' END AS authors"


async def _all(conn: psycopg.AsyncConnection, query: Any, params: Any = ()) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def _one(conn: psycopg.AsyncConnection, query: Any, params: Any = ()) -> dict | None:
    return await (await conn.execute(query, params)).fetchone()


# -- the status line -------------------------------------------------------------------------------


async def status(
    conn: psycopg.AsyncConnection, *, grading_attempts: int, triage_attempts: int
) -> dict[str, Any]:
    """The latest nightly run and what it left for Quality. Counts are of what waits now."""
    run = await _one(conn, "SELECT * FROM runs WHERE kind = 'nightly' ORDER BY id DESC LIMIT 1")
    clock = await _one(conn, "SELECT now() AS now")
    now = clock["now"]
    if run is None:
        return {"state": "none"}
    result: dict[str, Any] = {
        "run_id": run["id"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "read": (run["counts"] or {}).get("triaged", 0),
    }
    if run["finished_at"] is None and now - run["started_at"] < STALE_AFTER:
        return result | {"state": "running"}
    finished = run["finished_at"] or run["started_at"]
    if run["error"] or run["finished_at"] is None or now - finished > STALE_AFTER:
        last = await _one(
            conn,
            "SELECT day FROM issues WHERE kind = 'daily' ORDER BY day DESC LIMIT 1",
        )
        return result | {"state": "failed", "error": run["error"], "showing": last and last["day"]}
    waiting = {
        "splits": await _open_splits(conn),
        "ungraded": (
            await _one(
                conn,
                "SELECT count(*) AS n FROM candidates c WHERE state = 'pending' "
                "AND failed_runs >= %s AND NOT EXISTS (SELECT 1 FROM candidates n "
                "WHERE n.paper_id = c.paper_id AND n.topic = c.topic "
                "AND n.topic_revision > c.topic_revision)",
                (grading_attempts,),
            )
        )["n"],
        "awaiting_triage": (
            await _one(
                conn,
                "SELECT count(*) AS n FROM triage WHERE state = 'failed' AND failed_runs >= %s",
                (triage_attempts,),
            )
        )["n"],
    }
    return result | {"state": "attention" if any(waiting.values()) else "quiet"} | waiting


async def _open_splits(conn: psycopg.AsyncConnection) -> int:
    """Split verdicts in the latest daily issue whose two graders straddle the relevant line
    and on which the owner has given no word since (spec decision 14)."""
    rows = await _all(
        conn,
        """
        WITH latest AS (SELECT id FROM issues WHERE kind = 'daily' ORDER BY day DESC LIMIT 1),
        current AS (
            SELECT DISTINCT ON (v.paper_id, v.topic) v.*
            FROM verdicts v JOIN issue_entries e ON e.paper_id = v.paper_id AND e.topic = v.topic
            WHERE e.issue_id = (SELECT id FROM latest)
            ORDER BY v.paper_id, v.topic, v.paper_revision DESC, v.topic_revision DESC,
                     v.recorded_at DESC, v.id DESC)
        SELECT c.judge->>'grade' AS judge, c.check_opinion->>'grade' AS check
        FROM current c
        WHERE c.split
          AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.paper_id = c.paper_id
                          AND l.topic = c.topic AND l.value <> 'unsure' AND NOT l.provisional
                          AND l.labelled_at > c.recorded_at)
          AND NOT EXISTS (SELECT 1 FROM reactions r WHERE r.paper_id = c.paper_id
                          AND r.topic = c.topic AND r.reason IN ('unrelated', 'great_find')
                          AND r.reacted_at > c.recorded_at)
        """,
    )
    return sum((r["judge"] in SHOWN) != (r["check"] in SHOWN) for r in rows)


# -- issues ----------------------------------------------------------------------------------------


async def issue_feed(
    conn: psycopg.AsyncConnection, before: date | None, limit: int
) -> list[dict[str, Any]]:
    return await _all(
        conn,
        "SELECT id, kind, day, lede FROM issues WHERE kind = 'daily' "
        "AND (%s::date IS NULL OR day < %s) ORDER BY day DESC LIMIT %s",
        (before, before, limit),
    )


async def latest_weekly(conn: psycopg.AsyncConnection) -> dict[str, Any] | None:
    return await _one(
        conn, "SELECT id, kind, day, lede FROM issues WHERE kind = 'weekly' ORDER BY day DESC"
    )


async def issue(conn: psycopg.AsyncConnection, issue_id: int) -> dict[str, Any] | None:
    found = await _one(conn, "SELECT * FROM issues WHERE id = %s", (issue_id,))
    if found is None:
        return None
    entries = await _all(
        conn,
        f"""
        SELECT e.paper_id, e.topic, e.grade, e.position, k.text AS take,
               p.title, {AUTHORS}, p.published, p.version,
               p.revision > 1 AS revised, p.fetched_at AS added,
               g.grade AS grade_now,
               coalesce(l.saved, false) AS saved,
               r.value AS reaction, r.reason AS reaction_reason,
               (SELECT coalesce(array_agg(t2.name ORDER BY t2.position), '{{}}')
                FROM effective_grades g2 JOIN topics t2 ON t2.slug = g2.topic
                WHERE g2.paper_id = e.paper_id AND g2.topic <> e.topic
                  AND g2.grade IN ('must_read', 'relevant')) AS also_in
        FROM issue_entries e
        JOIN papers p ON p.id = e.paper_id
        JOIN topics t ON t.slug = e.topic
        LEFT JOIN takes k ON k.id = e.take_id
        LEFT JOIN effective_grades g ON g.paper_id = e.paper_id AND g.topic = e.topic
        LEFT JOIN library l ON l.paper_id = e.paper_id
        LEFT JOIN reactions r ON r.paper_id = e.paper_id AND r.topic = e.topic
        WHERE e.issue_id = %s
        ORDER BY t.position, array_position({GRADE_ORDER}, e.grade), e.position
        """,
        (issue_id,),
    )
    return found | {"entries": entries}


# -- a paper ---------------------------------------------------------------------------------------


async def paper(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """Everything the paper page shows, the folded "how this was chosen" included."""
    found = await _one(
        conn,
        f"SELECT p.id, p.title, p.abstract, {AUTHORS}, "
        "coalesce(p.categories, '{}') AS categories, p.published, p.updated, "
        "p.url, p.version, p.revision, p.version_history, p.doi, p.journal_ref, "
        "p.fetched_at AS added, coalesce(l.saved, false) AS saved, l.hand "
        "FROM papers p LEFT JOIN library l ON l.paper_id = p.id WHERE p.id = %s",
        (paper_id,),
    )
    if found is None:
        return None
    topics = await _all(
        conn,
        f"""
        SELECT t.slug, t.name, t.kind, t.colour, t.position, g.grade,
               (SELECT text FROM takes k WHERE k.paper_id = %(p)s AND k.topic = t.slug
                {FRESH} ORDER BY id DESC LIMIT 1) AS take,
               (SELECT row_to_json(l) FROM (SELECT value, grade, origin, provisional
                FROM labels WHERE paper_id = %(p)s AND topic = t.slug) l) AS label,
               (SELECT row_to_json(r) FROM (SELECT value, reason FROM reactions
                WHERE paper_id = %(p)s AND topic = t.slug) r) AS reaction,
               EXISTS (SELECT 1 FROM memberships m WHERE m.paper_id = %(p)s
                       AND m.topic = t.slug) AS member,
               EXISTS (SELECT 1 FROM candidates c WHERE c.paper_id = %(p)s AND c.topic = t.slug
                       AND c.state = 'pending') AS grading
        FROM topics t
        LEFT JOIN effective_grades g ON g.paper_id = %(p)s AND g.topic = t.slug
        WHERE t.status <> 'draft'
        ORDER BY array_position(ARRAY['watch', 'radar', 'pulse'], t.kind), t.position
        """,
        {"p": paper_id},
    )
    unattached = await _one(
        conn,
        "SELECT value, reason FROM reactions WHERE paper_id = %s AND topic IS NULL",
        (paper_id,),
    )
    triage = await _one(
        conn,
        "SELECT state, answers, model, triaged_at FROM triage WHERE paper_id = %s",
        (paper_id,),
    )
    verdicts = await _all(
        conn,
        "SELECT DISTINCT ON (topic) topic, grade, split, judge, check_opinion AS check, "
        "migrated, recorded_at FROM verdicts WHERE paper_id = %s "
        "ORDER BY topic, paper_revision DESC, topic_revision DESC, recorded_at DESC, id DESC",
        (paper_id,),
    )
    history = await _all(
        conn,
        """
        SELECT 'candidate' AS what, topic, created_at AS at, NULL AS grade
        FROM candidates WHERE paper_id = %(p)s
        UNION ALL
        SELECT 'graded', topic, recorded_at, grade FROM verdicts WHERE paper_id = %(p)s
        UNION ALL
        SELECT 'missed', topic, reported_at, NULL FROM misses WHERE paper_id = %(p)s
        ORDER BY at
        """,
        {"p": paper_id},
    )
    return found | {
        "topics": topics,
        "reaction": unattached,
        "triage": triage,
        "verdicts": verdicts,
        "history": history,
    }


# -- the papers table ------------------------------------------------------------------------------

SORTS = {
    "added": sql.SQL("p.fetched_at DESC, p.id"),
    "published": sql.SQL("p.published DESC NULLS LAST, p.id"),
    "title": sql.SQL("p.title, p.id"),
}


async def papers(
    conn: psycopg.AsyncConnection,
    *,
    q: str | None = None,
    topic: str | None = None,
    adjacent: bool = False,
    saved: bool | None = None,
    year: int | None = None,
    sort: str | None = None,
    limit: int | None = 50,
    offset: int = 0,
    ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """One page of the table, and how many rows the filter matches.

    With a Watch topic the table is that topic's papers at relevant or above (adjacent too
    when asked) with that topic's grade and take; with a field topic, its members; otherwise
    the whole collection, each row with its best Watch grade and the take of the first Watch
    topic, by the owner's order, that has one. `also_in` names the other topics the paper is
    relevant to, leaving out the one whose take is shown. A search sorts by published date.
    """
    kind = None
    if topic:
        found = await _one(conn, "SELECT kind FROM topics WHERE slug = %s", (topic,))
        kind = found["kind"] if found else "watch"
    where, params = [sql.SQL("true")], []
    if ids is not None:
        where.append(sql.SQL("p.id = ANY(%s)"))
        params.append(ids)
    if q:
        where.append(sql.SQL("p.search @@ websearch_to_tsquery('english', %s)"))
        params.append(q)
    if saved is not None:
        where.append(sql.SQL("coalesce(l.saved, false) = %s"))
        params.append(saved)
    if year:
        where.append(sql.SQL("extract(year FROM p.published) = %s"))
        params.append(year)
    if kind == "watch":
        grades = ["must_read", "relevant", *(["adjacent"] if adjacent else [])]
        scope = sql.SQL(
            "JOIN effective_grades g ON g.paper_id = p.id AND g.topic = %s AND g.grade = ANY(%s)"
        )
        scope_params = [topic, grades]
        grade = sql.SQL("g.grade")
        take = sql.SQL(
            "(SELECT k.text, k.topic FROM takes k WHERE k.paper_id = p.id AND k.topic = %s "
            f"{FRESH} ORDER BY id DESC LIMIT 1)"
        )
        take_params = [topic]
    else:
        if kind:
            scope = sql.SQL("JOIN memberships m ON m.paper_id = p.id AND m.topic = %s")
            scope_params = [topic]
        else:
            scope, scope_params = sql.SQL(""), []
        grade = sql.SQL(
            "(SELECT g.grade FROM effective_grades g WHERE g.paper_id = p.id "
            f"ORDER BY array_position({GRADE_ORDER}, g.grade) LIMIT 1)"
        )
        take = sql.SQL(
            "(SELECT k.text, k.topic FROM takes k JOIN topics t ON t.slug = k.topic "
            "JOIN effective_grades g ON g.paper_id = k.paper_id AND g.topic = k.topic "
            "WHERE k.paper_id = p.id AND g.grade IN ('must_read', 'relevant') "
            f"{FRESH} ORDER BY t.position, k.id DESC LIMIT 1)"
        )
        take_params = []
    order = SORTS.get(sort or ("published" if q else "added"), SORTS["added"])
    query = sql.SQL(
        """
        SELECT p.id, p.title, {authors}, p.published,
               p.fetched_at AS added,
               {grade} AS grade, tk.text AS take, tk.topic AS take_topic,
               coalesce(l.saved, false) AS saved,
               EXISTS (SELECT 1 FROM candidates c WHERE c.paper_id = p.id
                       AND c.state = 'pending') AS grading,
               (SELECT coalesce(array_agg(t2.name ORDER BY t2.position), '{{}}')
                FROM effective_grades g2 JOIN topics t2 ON t2.slug = g2.topic
                WHERE g2.paper_id = p.id AND g2.grade IN ('must_read', 'relevant')
                  AND g2.topic IS DISTINCT FROM coalesce(%s::text, tk.topic)) AS also_in,
               count(*) OVER () AS total
        FROM papers p {scope}
        LEFT JOIN LATERAL {take} tk ON true
        LEFT JOIN library l ON l.paper_id = p.id
        WHERE {where}
        ORDER BY {order} LIMIT %s OFFSET %s
        """
    ).format(
        authors=sql.SQL(AUTHORS),
        grade=grade,
        take=take,
        scope=scope,
        where=sql.SQL(" AND ").join(where),
        order=order,
    )
    rows = await _all(conn, query, [topic, *scope_params, *take_params, *params, limit, offset])
    if rows:
        total = rows[0]["total"]
    elif offset:  # past the last page: count the filter on its own
        total = (
            await papers(
                conn,
                q=q,
                topic=topic,
                adjacent=adjacent,
                saved=saved,
                year=year,
                limit=1,
                offset=0,
                ids=ids,
            )
        )[1]
    else:
        total = 0
    return [{k: v for k, v in r.items() if k != "total"} for r in rows], total


# -- topics ----------------------------------------------------------------------------------------


async def topic_cards(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    """Every topic with its counts and what is new since its page was last opened."""
    return await _all(
        conn,
        """
        SELECT t.slug, t.name, t.kind, t.status, t.colour, t.position, t.subject,
               v.opened_at,
               CASE WHEN t.kind = 'watch'
                    THEN (SELECT count(*) FROM effective_grades g WHERE g.topic = t.slug
                          AND g.grade IN ('must_read', 'relevant'))
                    ELSE (SELECT count(*) FROM memberships m WHERE m.topic = t.slug) END
                   AS papers,
               CASE WHEN t.kind = 'watch'
                    THEN (SELECT count(*) FROM effective_grades g JOIN library l
                          ON l.paper_id = g.paper_id AND l.saved
                          WHERE g.topic = t.slug AND g.grade IN ('must_read', 'relevant'))
                    ELSE (SELECT count(*) FROM memberships m JOIN library l
                          ON l.paper_id = m.paper_id AND l.saved WHERE m.topic = t.slug) END
                   AS saved,
               (SELECT count(*) FROM issue_entries e
                WHERE e.topic = t.slug AND e.added_at > coalesce(v.opened_at, t.created_at))
                   AS new,
               (SELECT count(*) FROM issue_entries e
                JOIN library l ON l.paper_id = e.paper_id AND l.saved
                WHERE e.topic = t.slug AND e.added_at > coalesce(v.opened_at, t.created_at))
                   AS new_saved
        FROM topics t LEFT JOIN topic_visits v ON v.topic = t.slug
        WHERE t.status <> 'draft'
        ORDER BY array_position(ARRAY['watch', 'radar', 'pulse'], t.kind), t.position
        """,
    )


async def visit(conn: psycopg.AsyncConnection, topic: str) -> None:
    await conn.execute(
        "INSERT INTO topic_visits (topic) VALUES (%s) "
        "ON CONFLICT (topic) DO UPDATE SET opened_at = now()",
        (topic,),
    )
