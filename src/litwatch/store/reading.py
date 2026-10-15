"""The queries behind the reading side: the status line, the paper lists (the whole collection
and a topic's page), the topic cards and a paper's page. Read-only; every grade here is the
effective one.

A list is two queries over one filter, then the page's rows: how many papers each level (rarity,
and grade on a topic page) holds, for the shelves and the rarity bar, and the page of ids in the
list's order. A topic's papers are its subtree's (pipeline.md, section 4, what a parent topic
shows): those a standing membership puts in a class under it (the uncertain band and the
keyword check are for grading only), and those graded or given the owner's word on it or on a
followed topic under it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

import psycopg

from litwatch.core import FAILED_RUNS, Depth, Rarity, Taxonomy, Weight
from litwatch.core.grading import NO_WEIGHT, WEIGHT_FLOORS
from litwatch.store import failures
from litwatch.store.analyses import CURRENT_GIST

STALE_AFTER = timedelta(hours=36)
# A stored JSON null in `authors` reads as no authors.
AUTHORS = "CASE WHEN jsonb_typeof(p.authors) = 'array' THEN p.authors ELSE '[]' END"
# Each rarity's place, low to high, for sorting and picking the highest.
LEVELS = "ARRAY[" + ", ".join(f"'{r.value}'" for r in Rarity) + "]::text[]"


def _rank(column: str) -> str:
    return f"array_position({LEVELS}, {column})"


# The level an ungraded paper's Weight gives it, from `rarities r` (none outside the collection).
FLOOR = (
    f"CASE WHEN r.paper_id IS NOT NULL THEN CASE coalesce(r.weight, '{NO_WEIGHT.value}') "
    + " ".join(f"WHEN '{w.value}' THEN '{level.value}'" for w, level in WEIGHT_FLOORS.items())
    + " END END"
)
# A membership `m` that puts its paper in its class on a page: one that stands (the owner's
# label on a class outranks every other source for it; core.classification.standing) and is not
# for grading only.
# A grading failure waits on the owner only while its topic is active (`store.failures`).
ACTIVE = (
    "(f.topic = '' OR EXISTS (SELECT 1 FROM topics t WHERE t.class = f.topic "
    "AND t.status = 'active'))"
)
SHOWN_MEMBER = """
    m.member AND m.source IN ('classifier', 'owner', 'miss')
    AND (m.source = 'owner' OR NOT EXISTS (
        SELECT 1 FROM memberships o WHERE o.paper_id = m.paper_id AND o.class = m.class
        AND o.source = 'owner'))
"""
# The papers on each scope topic's page (`pairs`)...
SCOPE = f"""
map AS (SELECT * FROM unnest(%(map_classes)s::text[], %(map_topics)s::text[])
        AS map (class, topic)),
up AS (SELECT * FROM unnest(%(up_below)s::text[], %(up_topics)s::text[]) AS up (below, topic)),
pairs AS (
    SELECT map.topic, m.paper_id FROM memberships m JOIN map ON map.class = m.class
    WHERE {SHOWN_MEMBER}
    UNION
    SELECT up.topic, g.paper_id FROM effective_grades g JOIN up ON up.below = g.topic)
"""
# ...and how each shows there (`standing`): the topic's own grade, else its best followed
# descendant's (`via`), else ungraded at its Weight's floor (core.grading.page_standing, in SQL).
STANDING = f""",
best AS (
    SELECT DISTINCT ON (up.topic, g.paper_id) up.topic, g.paper_id, g.topic AS via, g.grade,
           g.rarity
    FROM effective_grades g JOIN up ON up.below = g.topic AND up.below <> up.topic
    ORDER BY up.topic, g.paper_id, {_rank("g.rarity")} DESC,
             array_position(%(order)s::text[], g.topic)),
standing AS (
    SELECT s.topic, s.paper_id, coalesce(own.grade, best.grade) AS grade,
           coalesce(own.rarity, best.rarity, {FLOOR}) AS rarity,
           CASE WHEN own.paper_id IS NULL THEN best.via END AS via
    FROM pairs s
    LEFT JOIN effective_grades own ON own.paper_id = s.paper_id AND own.topic = s.topic
    LEFT JOIN best ON best.topic = s.topic AND best.paper_id = s.paper_id
    LEFT JOIN rarities r ON r.paper_id = s.paper_id)
"""

type Sort = Literal["rarest", "newest", "added", "title", "best"]
# A grade, or `ungraded`: no grade on the topics in view.
type GradeLevel = Literal["must_read", "relevant", "adjacent", "none", "ungraded"]
UNGRADED = "ungraded"
GRADE_LEVELS: tuple[str, ...] = ("must_read", "relevant", "adjacent", "none", UNGRADED)


@dataclass
class Query:
    """What a list shows. Papers: the whole collection, or the pages of `topics` (each with its
    subtree) or of every topic at `depth`. Rarity and grade are the levels, which the counts
    leave out. `ids` keeps the list to those papers."""

    q: str | None = None
    topics: list[str] = field(default_factory=list)
    depth: Depth | None = None
    rarity: list[Rarity] = field(default_factory=list)
    grade: list[GradeLevel] = field(default_factory=list)
    saved: bool | None = None
    code: bool | None = None  # the abstract announces released code
    weight: list[Weight] = field(default_factory=list)
    since: date | None = None  # published on or after, a UTC day
    until: date | None = None  # published on or before
    sort: Sort = "rarest"
    ids: list[str] | None = None


@dataclass
class Listed:
    """One page of a list: the rows in order, how many papers the whole filter matches, and how
    many each level holds under every filter but the levels (`unrated`: no rarity yet)."""

    rows: list[dict[str, Any]]
    total: int
    rarities: dict[str, int]
    grades: dict[str, int]


async def _all(conn: psycopg.AsyncConnection, query: Any, params: Any = ()) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def _one(conn: psycopg.AsyncConnection, query: Any, params: Any = ()) -> dict | None:
    return await (await conn.execute(query, params)).fetchone()


# -- the status line -------------------------------------------------------------------------------


async def status(conn: psycopg.AsyncConnection) -> dict[str, Any]:
    """The latest nightly run: running, failed (or stuck past `STALE_AFTER`), or done; done
    with papers awaiting classification, or with analysis or a grading stopped, needs the
    owner's attention. `papers` are those the run classified; while it runs, `waiting` are
    those still to classify."""
    run = await _one(conn, "SELECT * FROM runs WHERE kind = 'nightly' ORDER BY id DESC LIMIT 1")
    if run is None:
        return {"state": "none"}
    now = (await _one(conn, "SELECT now() AS now"))["now"]
    awaiting = await failures.count_stuck(conn, "classification", FAILED_RUNS)
    analysis = await failures.count_stuck(conn, "analysis", FAILED_RUNS)
    ungraded = await failures.count_stuck(conn, "grading", FAILED_RUNS)
    papers = await _one(
        conn,
        "SELECT count(*) AS n FROM classifications WHERE classified_at >= %(from)s "
        "AND (%(to)s::timestamptz IS NULL OR classified_at <= %(to)s)",
        {"from": run["started_at"], "to": run["finished_at"]},
    )
    result: dict[str, Any] = {
        "run_id": run["id"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "papers": papers["n"],
        "awaiting_classification": awaiting,
        "analysis_failed": analysis,
        "ungraded": ungraded,
    }
    if run["finished_at"] is None and now - run["started_at"] < STALE_AFTER:
        waiting = await _one(
            conn,
            """
            SELECT count(*) AS n FROM papers p
            LEFT JOIN step_failures f ON f.paper_id = p.id AND f.step = 'classification'
                 AND f.paper_revision = p.revision
            WHERE p.dismissed_at IS NULL AND coalesce(f.failed_runs, 0) < %s
              AND NOT EXISTS (SELECT 1 FROM classifications c
                              WHERE c.paper_id = p.id AND c.paper_revision = p.revision)
            """,
            (FAILED_RUNS,),
        )
        return result | {"state": "running", "waiting": waiting["n"]}
    if run["error"] or run["finished_at"] is None:
        return result | {"state": "failed", "error": run["error"]}
    return result | {"state": "attention" if awaiting or analysis or ungraded else "quiet"}


# -- lists -----------------------------------------------------------------------------------------


def _scope(tree: Taxonomy | None, topics: list[str]) -> dict[str, Any]:
    """The parameters of `SCOPE` and `STANDING` for the pages of `topics`, shown topics of
    `tree`."""
    found: dict[str, Any] = {k: [] for k in ("map_classes", "map_topics", "up_below", "up_topics")}
    for slug in topics:
        for c in tree.subtree(slug):
            found["map_classes"].append(c)
            found["map_topics"].append(slug)
        for t in tree.under(slug):
            found["up_below"].append(t.slug)
            found["up_topics"].append(slug)
    return found | {"order": [t.slug for t in tree.shown()] if tree else []}


def _filters(query: Query) -> tuple[list[str], dict[str, Any]]:
    """Conditions on `papers p`, `rarities r` and `library l` for every filter but the levels."""
    # A dismissed paper is out of every list, unless asked for by id.
    where = ["p.dismissed_at IS NULL" if query.ids is None else "p.id = ANY(%(ids)s)"]
    params: dict[str, Any] = {} if query.ids is None else {"ids": query.ids}
    if query.q:
        where.append("p.search @@ websearch_to_tsquery('english', %(q)s)")
        params["q"] = query.q
    if query.saved is not None:
        where.append("coalesce(l.saved, false) = %(saved)s")
        params["saved"] = query.saved
    if query.code is not None:
        # One analysis per revision: analysis runs only on a revision that has none.
        where.append(
            "EXISTS (SELECT 1 FROM analyses a WHERE a.paper_id = p.id "
            "AND a.paper_revision = p.revision AND a.releases_code) = %(code)s"
        )
        params["code"] = query.code
    if query.weight:
        where.append("r.weight = ANY(%(weight)s)")
        params["weight"] = [w.value for w in query.weight]
    if query.since:
        where.append("p.published >= %(since)s::timestamp AT TIME ZONE 'UTC'")
        params["since"] = query.since
    if query.until:
        where.append("p.published < %(until)s::timestamp AT TIME ZONE 'UTC'")
        params["until"] = query.until + timedelta(days=1)
    return where, params


def _levels(
    query: Query, rarity: str, graded: str, ungraded: str
) -> tuple[list[str], dict[str, Any]]:
    """Conditions for the level filters: `rarity` is the level's column, `graded` and
    `ungraded` say a paper has one of `%(grades)s` or no grade at all."""
    where, params = [], {}
    if query.rarity:
        where.append(f"{rarity} = ANY(%(rarity)s)")
        params["rarity"] = [r.value for r in query.rarity]
    named = [g for g in query.grade if g != UNGRADED]
    either = ([graded] if named else []) + ([ungraded] if UNGRADED in query.grade else [])
    if either:
        where.append("(" + " OR ".join(either) + ")")
        params["grades"] = named
    return where, params


def _order(query: Query, rarity: str) -> str:
    if query.sort == "best" and query.q:
        return "ts_rank(p.search, websearch_to_tsquery('english', %(q)s)) DESC, p.id"
    return {
        "newest": "p.published DESC NULLS LAST, p.id",
        "added": "p.fetched_at DESC, p.id",
        "title": "p.title, p.id",
    }.get(query.sort, f"{_rank(rarity)} DESC NULLS LAST, p.published DESC NULLS LAST, p.id")


async def _list(
    conn: psycopg.AsyncConnection,
    query: Query,
    *,
    ctes: str,
    source: str,
    where: list[str],
    levels: list[str],
    params: dict[str, Any],
    rarity: str,
    grade: str,
    extra: str = "",
    limit: int | None,
    offset: int,
) -> tuple[list[dict[str, Any]], int, dict[str, int], dict[str, int]]:
    """The page (ids, and the `extra` columns) in the list's order, the total, and the counts
    per rarity and per grade, for one filter. `source` holds `papers p`, `rarities r` and
    `library l`; `rarity` and `grade` are the levels' columns."""
    on = " AND ".join(where)
    hits = " AND ".join(levels) or "true"
    with_ = f"WITH {ctes}" if ctes else ""
    counted = await _all(
        conn,
        f"""
        {with_}
        SELECT {rarity} AS rarity, {grade} AS grade, count(*) AS n,
               count(*) FILTER (WHERE {hits}) AS hits
        FROM {source} WHERE {on} GROUP BY 1, 2
        """,
        params,
    )
    page = await _all(
        conn,
        f"""
        {with_}
        SELECT p.id{extra} FROM {source} WHERE {on} AND {hits}
        ORDER BY {_order(query, rarity)} LIMIT %(limit)s OFFSET %(offset)s
        """,
        params | {"limit": limit, "offset": offset},
    )
    rarities = dict.fromkeys([r.value for r in reversed(Rarity)], 0) | {"unrated": 0}
    grades = dict.fromkeys(GRADE_LEVELS, 0)
    for row in counted:
        rarities[row["rarity"] or "unrated"] += row["n"]
        grades[row["grade"] or UNGRADED] += row["n"]
    return page, sum(row["hits"] for row in counted), rarities, grades


async def papers(
    conn: psycopg.AsyncConnection,
    tree: Taxonomy | None,
    query: Query,
    *,
    limit: int | None = 50,
    offset: int = 0,
) -> Listed:
    """A page of the collection (Papers, search, lookups), each row at its mixed-list rarity.
    With topics or a depth, only their pages' papers. A grade filter asks for a grade (a
    badge's) on a topic in view: those topics and the followed topics under them, or any."""
    shown = tree.shown() if tree else []
    where, params = _filters(query)
    in_view = [t.slug for t in shown]
    ctes = ""
    if query.topics or query.depth:
        chosen = [
            t.slug
            for t in shown
            if (not query.topics or t.slug in query.topics)
            and (query.depth is None or t.depth == query.depth)
        ]
        ctes = SCOPE
        params |= _scope(tree, chosen)
        where.append("p.id IN (SELECT paper_id FROM pairs)")
        in_view = list(dict.fromkeys(params["up_below"]))
    params["in_view"] = in_view
    graded = (
        "EXISTS (SELECT 1 FROM effective_grades e WHERE e.paper_id = p.id "
        "AND e.topic = ANY(%(in_view)s) AND e.grade = ANY(%(grades)s))"
    )
    ungraded = (
        "NOT EXISTS (SELECT 1 FROM effective_grades e WHERE e.paper_id = p.id "
        "AND e.topic = ANY(%(in_view)s))"
    )
    levels, more = _levels(query, "r.level", graded, ungraded)
    page, total, rarities, _ = await _list(
        conn,
        query,
        ctes=ctes,
        source="papers p LEFT JOIN rarities r ON r.paper_id = p.id "
        "LEFT JOIN library l ON l.paper_id = p.id",
        where=where,
        levels=levels,
        params=params | more,
        rarity="r.level",
        grade="NULL::text",
        limit=limit,
        offset=offset,
    )
    rows = await _rows(conn, [r["id"] for r in page], in_view)
    return Listed(rows=rows, total=total, rarities=rarities, grades={})


async def topic_papers(
    conn: psycopg.AsyncConnection,
    tree: Taxonomy,
    slug: str,
    query: Query,
    *,
    limit: int | None = 50,
    offset: int = 0,
) -> Listed:
    """A page of one topic's papers, each with how it shows there: `grade`, `rarity` (the topic
    page's) and `via`. The levels filter and count by those."""
    where, params = _filters(query)
    params |= _scope(tree, [slug])
    levels, more = _levels(query, "st.rarity", "st.grade = ANY(%(grades)s)", "st.grade IS NULL")
    page, total, rarities, grades = await _list(
        conn,
        query,
        ctes=SCOPE + STANDING,
        source="standing st JOIN papers p ON p.id = st.paper_id "
        "LEFT JOIN rarities r ON r.paper_id = p.id LEFT JOIN library l ON l.paper_id = p.id",
        where=where,
        levels=levels,
        params=params | more,
        rarity="st.rarity",
        grade="st.grade",
        extra=", st.grade, st.rarity, st.via",
        limit=limit,
        offset=offset,
    )
    in_view = list(dict.fromkeys(params["up_below"]))
    rows = await _rows(conn, [r["id"] for r in page], in_view)
    for row, standing in zip(rows, page, strict=True):
        row |= {k: standing[k] for k in ("grade", "rarity", "via")}
    return Listed(rows=rows, total=total, rarities=rarities, grades=grades)


async def _rows(conn: psycopg.AsyncConnection, ids: list[str], in_view: list[str]) -> list[dict]:
    """The rows of a page, in its order: each paper with its current Gist, Weight and facets,
    its mixed-list rarity, its effective grades (`grades`, for its badges), whether it is saved,
    and its open state on an `in_view` topic or its own: `word` (a split or escalated verdict
    the owner has not labelled), else `failure` (a step stopped after `FAILED_RUNS` failed runs:
    classification, analysis, or a grading)."""
    if not ids:
        return []
    found = await _all(
        conn,
        f"""
        SELECT p.id, p.title, {AUTHORS} AS authors, p.published, r.level AS rarity, r.weight,
               coalesce(l.saved, false) AS saved, {CURRENT_GIST} AS gist,
               a.contribution, a.releases_code, a.releases_weights,
               (SELECT coalesce(jsonb_agg(jsonb_build_object(
                    'topic', e.topic, 'grade', e.grade, 'rarity', e.rarity)), '[]')
                FROM effective_grades e WHERE e.paper_id = p.id) AS grades,
               CASE WHEN EXISTS (
                        SELECT 1 FROM (
                            SELECT DISTINCT ON (v.topic) v.topic, v.split, v.escalated
                            FROM verdicts v WHERE v.paper_id = p.id AND v.topic = ANY(%(in_view)s)
                            ORDER BY v.topic, v.paper_revision DESC, v.recorded_at DESC, v.id DESC
                        ) cv
                        WHERE (cv.split OR cv.escalated) AND NOT EXISTS (
                            SELECT 1 FROM labels lb WHERE lb.paper_id = p.id
                            AND lb.topic = cv.topic AND NOT lb.provisional))
                    THEN 'word'
                    WHEN EXISTS (
                        SELECT 1 FROM step_failures f WHERE f.paper_id = p.id
                        AND f.paper_revision = p.revision AND f.failed_runs >= %(runs)s
                        AND (f.step <> 'grading' OR f.topic = ANY(%(in_view)s))
                        AND {ACTIVE})
                    THEN 'failure' END AS state
        FROM papers p
        LEFT JOIN rarities r ON r.paper_id = p.id
        LEFT JOIN library l ON l.paper_id = p.id
        LEFT JOIN LATERAL (
            SELECT contribution, releases_code, releases_weights FROM analyses
            WHERE paper_id = p.id AND paper_revision = p.revision ORDER BY id DESC LIMIT 1
        ) a ON true
        WHERE p.id = ANY(%(ids)s)
        """,
        {"ids": ids, "in_view": in_view, "runs": FAILED_RUNS},
    )
    by_id = {r["id"]: r for r in found}
    return [by_id[i] for i in ids]


# -- topics ----------------------------------------------------------------------------------------


async def topic_cards(conn: psycopg.AsyncConnection, tree: Taxonomy) -> dict[str, dict[str, Any]]:
    """Per shown topic: how many papers its page holds, how many of them are saved, how many at
    each of the page's rarity levels, and its newest must read."""
    slugs = [t.slug for t in tree.shown()]
    params = _scope(tree, slugs)
    levels = [r.value for r in reversed(Rarity)]
    cards: dict[str, dict[str, Any]] = {
        s: {"papers": 0, "saved": 0, "rarities": dict.fromkeys(levels, 0), "newest": None}
        for s in slugs
    }
    counted = await _all(
        conn,
        f"""
        WITH {SCOPE}{STANDING}
        SELECT st.topic, st.rarity, count(*) AS n, count(*) FILTER (WHERE l.saved) AS saved
        FROM standing st JOIN papers p ON p.id = st.paper_id AND p.dismissed_at IS NULL
        LEFT JOIN library l ON l.paper_id = p.id
        GROUP BY 1, 2
        """,
        params,
    )
    for row in counted:
        card = cards[row["topic"]]
        card["papers"] += row["n"]
        card["saved"] += row["saved"]
        if row["rarity"]:
            card["rarities"][row["rarity"]] += row["n"]
    newest = await _all(
        conn,
        f"""
        WITH {SCOPE}{STANDING}
        SELECT DISTINCT ON (st.topic) st.topic, p.id, p.title
        FROM standing st JOIN papers p ON p.id = st.paper_id AND p.dismissed_at IS NULL
        WHERE st.grade = 'must_read'
        ORDER BY st.topic, p.published DESC NULLS LAST, p.id
        """,
        params,
    )
    for row in newest:
        cards[row["topic"]]["newest"] = {"id": row["id"], "title": row["title"]}
    return cards


# -- a paper ---------------------------------------------------------------------------------------


async def paper(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """What a paper's page is built from, for every topic, followed or not: the paper with its
    library state, rarity, current Weight, Gist and facets; its effective grades, current
    verdicts, labels, reactions and misses; its newest classification and its memberships; the
    steps failing on its current text; and its history."""
    found = await _one(
        conn,
        f"""
        SELECT p.id, p.title, p.abstract, {AUTHORS} AS authors, p.published, p.updated,
               p.version, p.revision, p.version_history,
               coalesce(l.saved, false) AS saved, l.hand, r.level AS rarity,
               {CURRENT_GIST} AS gist,
               EXISTS (SELECT 1 FROM classifications c WHERE c.paper_id = p.id) AS in_collection,
               w.weight, w.reason AS weight_reason, w.source AS weight_source,
               a.contribution, a.releases_code, a.releases_weights
        FROM papers p
        LEFT JOIN library l ON l.paper_id = p.id
        LEFT JOIN rarities r ON r.paper_id = p.id
        LEFT JOIN LATERAL (
            SELECT weight, reason, source FROM weights WHERE paper_id = p.id
            AND paper_revision = p.revision ORDER BY id DESC LIMIT 1
        ) w ON true
        LEFT JOIN LATERAL (
            SELECT contribution, releases_code, releases_weights FROM analyses
            WHERE paper_id = p.id AND paper_revision = p.revision ORDER BY id DESC LIMIT 1
        ) a ON true
        WHERE p.id = %s
        """,
        (paper_id,),
    )
    if found is None:
        return None
    p = {"p": paper_id}
    queries = {
        "grades": "SELECT topic, fit, grade, rarity FROM effective_grades WHERE paper_id = %(p)s",
        # the current verdict per topic
        "verdicts": "SELECT DISTINCT ON (topic) topic, fit, reason, opinions, split, escalated, "
        "paper_revision, recorded_at FROM verdicts WHERE paper_id = %(p)s "
        "ORDER BY topic, paper_revision DESC, recorded_at DESC, id DESC",
        "labels": "SELECT topic, value, origin, provisional, labelled_at FROM labels "
        "WHERE paper_id = %(p)s",
        "reactions": "SELECT topic, value, reason, reacted_at FROM reactions "
        "WHERE paper_id = %(p)s",
        "misses": "SELECT topic, reported_at FROM misses WHERE paper_id = %(p)s",
        "memberships": "SELECT class, source, member, probability FROM memberships "
        "WHERE paper_id = %(p)s ORDER BY class, source",
        "failures": "SELECT f.step, nullif(f.topic, '') AS topic, f.failed_runs, f.last_error "
        "FROM step_failures f JOIN papers p ON p.id = f.paper_id "
        "AND p.revision = f.paper_revision AND p.dismissed_at IS NULL "
        f"WHERE f.paper_id = %(p)s AND {ACTIVE}",
        "history": """
            SELECT 'graded' AS what, recorded_at AS at, topic, fit AS value, NULL AS reason,
                   paper_revision AS revision, split, escalated
            FROM verdicts WHERE paper_id = %(p)s
            UNION ALL
            SELECT 'labelled', labelled_at, topic, value, NULL, NULL, NULL, NULL
            FROM labels WHERE paper_id = %(p)s AND origin <> 'miss'  -- the miss says it
            UNION ALL
            SELECT 'reacted', reacted_at, topic, value, reason, NULL, NULL, NULL
            FROM reactions WHERE paper_id = %(p)s
            UNION ALL
            SELECT 'missed', reported_at, topic, NULL, NULL, NULL, NULL, NULL
            FROM misses WHERE paper_id = %(p)s
            UNION ALL
            SELECT 'classified', classified_at, primary_class, NULL, NULL, paper_revision,
                   NULL, NULL
            FROM classifications WHERE paper_id = %(p)s
            ORDER BY at, what""",
    }
    for key, query in queries.items():
        found[key] = await _all(conn, query, p)
    found["classification"] = await _one(
        conn,
        "SELECT c.primary_class, k.name AS primary_name, c.model, c.classified_at, "
        "c.paper_revision FROM classifications c JOIN classes k ON k.slug = c.primary_class "
        "WHERE c.paper_id = %(p)s ORDER BY c.paper_revision DESC, c.id DESC LIMIT 1",
        p,
    )
    return found


async def class_names(conn: psycopg.AsyncConnection, slugs: list[str]) -> dict[str, str]:
    """The names of classes, retired ones included."""
    rows = await _all(conn, "SELECT slug, name FROM classes WHERE slug = ANY(%s)", (slugs,))
    return {r["slug"]: r["name"] for r in rows}


async def in_library(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """What an integration acting on the library reads of a paper: whether it is saved, and its
    effective grade per Focus topic (`focus`: slug, name, grade) in the owner's order."""
    found = await _one(
        conn,
        "SELECT coalesce(l.saved, false) AS saved FROM papers p "
        "LEFT JOIN library l ON l.paper_id = p.id WHERE p.id = %s",
        (paper_id,),
    )
    if found is None:
        return None
    found["focus"] = await _all(
        conn,
        "SELECT t.class AS slug, c.name, g.grade FROM effective_grades g "
        "JOIN topics t ON t.class = g.topic AND t.depth = 'focus' AND t.status <> 'draft' "
        "JOIN classes c ON c.slug = t.class WHERE g.paper_id = %s ORDER BY t.position",
        (paper_id,),
    )
    return found
