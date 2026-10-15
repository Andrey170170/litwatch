"""The queries behind the reading side: the status line, a paper's page, the papers table
and the topic cards. Read-only; every grade here is the effective one."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg
from psycopg import sql

GRADE_ORDER = "ARRAY['must_read', 'relevant', 'adjacent', 'none']"
STALE_AFTER = timedelta(hours=36)
# A stored JSON null in `authors` reads as no authors.
AUTHORS = "CASE WHEN jsonb_typeof(p.authors) = 'array' THEN p.authors ELSE '[]' END AS authors"


async def _all(conn: psycopg.AsyncConnection, query: Any, params: Any = ()) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def _one(conn: psycopg.AsyncConnection, query: Any, params: Any = ()) -> dict | None:
    return await (await conn.execute(query, params)).fetchone()


# -- the status line -------------------------------------------------------------------------------


async def status(conn: psycopg.AsyncConnection) -> dict[str, Any]:
    """The latest nightly run: running, failed (or stuck past `STALE_AFTER`), or done."""
    run = await _one(conn, "SELECT * FROM runs WHERE kind = 'nightly' ORDER BY id DESC LIMIT 1")
    if run is None:
        return {"state": "none"}
    now = (await _one(conn, "SELECT now() AS now"))["now"]
    result: dict[str, Any] = {
        "run_id": run["id"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
    }
    if run["finished_at"] is None and now - run["started_at"] < STALE_AFTER:
        return result | {"state": "running"}
    if run["error"] or run["finished_at"] is None:
        return result | {"state": "failed", "error": run["error"]}
    return result | {"state": "quiet"}


# -- a paper ---------------------------------------------------------------------------------------


async def paper(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """Everything the paper page shows, each Focus topic's current verdict included."""
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
        """
        SELECT t.class AS slug, c.name, t.depth, t.colour, t.position, g.grade,
               (SELECT row_to_json(l) FROM (SELECT value, grade, origin, provisional
                FROM labels WHERE paper_id = %(p)s AND topic = t.class) l) AS label,
               (SELECT row_to_json(r) FROM (SELECT value, reason FROM reactions
                WHERE paper_id = %(p)s AND topic = t.class) r) AS reaction,
               EXISTS (SELECT 1 FROM misses ms WHERE ms.paper_id = %(p)s AND ms.topic = t.class)
                   AND EXISTS (SELECT 1 FROM labels lb WHERE lb.paper_id = %(p)s
                               AND lb.topic = t.class AND lb.value = 'relevant')
                   -- and the owner's current word still says so (a newer thumb can withdraw it)
                   AND coalesce(g.grade IN ('must_read', 'relevant'), false) AS missed
        FROM topics t JOIN classes c ON c.slug = t.class
        LEFT JOIN effective_grades g ON g.paper_id = %(p)s AND g.topic = t.class
        WHERE t.status <> 'draft'
        ORDER BY t.position
        """,
        {"p": paper_id},
    )
    unattached = await _one(
        conn,
        "SELECT value, reason FROM reactions WHERE paper_id = %s AND topic IS NULL",
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
        SELECT 'graded' AS what, topic, recorded_at AS at, grade
        FROM verdicts WHERE paper_id = %(p)s
        UNION ALL
        SELECT 'missed', topic, reported_at, NULL FROM misses WHERE paper_id = %(p)s
        ORDER BY at
        """,
        {"p": paper_id},
    )
    return found | {
        "topics": topics,
        "reaction": unattached,
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

    With a topic the table is that topic's papers at relevant or above (adjacent too when
    asked) with that topic's grade; otherwise the whole collection, each row with its best
    Focus grade. `also_in` names the other topics the paper is relevant to. A search sorts by
    published date.
    """
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
    if topic:
        grades = ["must_read", "relevant", *(["adjacent"] if adjacent else [])]
        scope = sql.SQL(
            "JOIN effective_grades g ON g.paper_id = p.id AND g.topic = %s AND g.grade = ANY(%s)"
        )
        scope_params = [topic, grades]
        grade = sql.SQL("g.grade")
    else:
        scope, scope_params = sql.SQL(""), []
        grade = sql.SQL(
            "(SELECT g.grade FROM effective_grades g WHERE g.paper_id = p.id "
            f"ORDER BY array_position({GRADE_ORDER}, g.grade) LIMIT 1)"
        )
    order = SORTS.get(sort or ("published" if q else "added"), SORTS["added"])
    query = sql.SQL(
        """
        SELECT p.id, p.title, {authors}, p.published,
               p.fetched_at AS added,
               {grade} AS grade,
               coalesce(l.saved, false) AS saved,
               (SELECT coalesce(array_agg(c2.name ORDER BY t2.position), '{{}}')
                FROM effective_grades g2 JOIN topics t2 ON t2.class = g2.topic
                JOIN classes c2 ON c2.slug = t2.class
                WHERE g2.paper_id = p.id AND g2.grade IN ('must_read', 'relevant')
                  AND g2.topic IS DISTINCT FROM %s::text) AS also_in,
               count(*) OVER () AS total
        FROM papers p {scope}
        LEFT JOIN library l ON l.paper_id = p.id
        WHERE {where}
        ORDER BY {order} LIMIT %s OFFSET %s
        """
    ).format(
        authors=sql.SQL(AUTHORS),
        grade=grade,
        scope=scope,
        where=sql.SQL(" AND ").join(where),
        order=order,
    )
    rows = await _all(conn, query, [topic, *scope_params, *params, limit, offset])
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
    """Every topic with how many papers it has at relevant or above, and how many of them
    are saved."""
    return await _all(
        conn,
        """
        SELECT t.class AS slug, c.name, t.depth, t.status, t.colour, t.position, t.subject,
               v.opened_at,
               (SELECT count(*) FROM effective_grades g WHERE g.topic = t.class
                AND g.grade IN ('must_read', 'relevant')) AS papers,
               (SELECT count(*) FROM effective_grades g JOIN library l
                ON l.paper_id = g.paper_id AND l.saved
                WHERE g.topic = t.class AND g.grade IN ('must_read', 'relevant')) AS saved
        FROM topics t JOIN classes c ON c.slug = t.class
        LEFT JOIN topic_visits v ON v.topic = t.class
        WHERE t.status <> 'draft'
        ORDER BY t.position
        """,
    )


async def visit(conn: psycopg.AsyncConnection, topic: str) -> None:
    await conn.execute(
        "INSERT INTO topic_visits (topic) VALUES (%s) "
        "ON CONFLICT (topic) DO UPDATE SET opened_at = now()",
        (topic,),
    )
