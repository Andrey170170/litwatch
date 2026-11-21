"""The queries behind Settings: Quality's lists, the misses and their traces, the runs, and
the counts the Topics and Taxonomy pages show."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any, LiteralString

from litwatch.core import Taxonomy
from litwatch.core.classification import keyword_hit, with_ancestors
from litwatch.core.evaluation import recall
from litwatch.gates.database import ReadUnit
from litwatch.store import evaluation
from litwatch.store.reading import SHOWN_MEMBER
from litwatch.store.verdicts import ASKS_WORD


async def _all(unit: ReadUnit, query: str, params: Any = ()) -> list[dict]:
    return await (await unit.execute(query, params)).fetchall()


async def last_night(unit: ReadUnit) -> dict[str, Any] | None:
    return await (
        await unit.execute("SELECT * FROM runs WHERE kind = 'nightly' ORDER BY id DESC LIMIT 1")
    ).fetchone()


def splits_window(night: dict[str, Any] | None) -> datetime | date:
    """Since when Needs you lists splits, on every surface that counts or shows them: last
    night's run, or the last day while no night has run yet."""
    return night["started_at"] if night else date.today() - timedelta(days=1)


def _asking(columns: LiteralString) -> LiteralString:
    """The verdicts asking for the owner's word (`ASKS_WORD`) recorded since the query's `since`
    (passed twice) that are still current, with `columns` of each: Focus splits, Area
    escalations, central or related grades from one lab, on the paper's current text (a verdict
    on an older revision waits for the regrading, not the owner), followed topics only."""
    return f"""
        SELECT {columns} FROM (
            SELECT DISTINCT ON (v.paper_id, v.topic) v.paper_id, v.topic, t.depth, v.fit,
                   v.reason, v.split, v.escalated, v.one_lab, v.opinions, v.recorded_at, p.title,
                   v.paper_revision AS revision
            FROM verdicts v JOIN papers p ON p.id = v.paper_id AND p.revision = v.paper_revision
                 AND NOT p.dropped
            JOIN topics t ON t.class = v.topic
            WHERE v.paper_id IN (SELECT paper_id FROM verdicts WHERE recorded_at >= %s)
            ORDER BY v.paper_id, v.topic, v.paper_revision DESC, v.recorded_at DESC,
                     v.id DESC) current
        WHERE {ASKS_WORD} AND recorded_at >= %s
    """


async def splits_since(unit: ReadUnit, since: Any) -> list[dict[str, Any]]:
    """The verdicts asking for the owner's word since `since` (`_asking`), newest first, with
    every grader's answer and the owner's label, for the owner to settle if they want to.
    `revision` is the text the verdict is on."""
    label = (
        "(SELECT row_to_json(l) FROM (SELECT value FROM labels"
        " WHERE paper_id = current.paper_id AND topic = current.topic) l) AS label"
    )
    return await _all(
        unit, _asking(f"current.*, {label}") + " ORDER BY recorded_at DESC", (since, since)
    )


async def count_unlabelled_splits(unit: ReadUnit, since: Any) -> int:
    """How many of the verdicts `splits_since` lists the owner has not labelled: those Needs you
    waits on."""
    query = _asking("count(*) AS n") + (
        " AND NOT EXISTS (SELECT 1 FROM labels l"
        " WHERE l.paper_id = current.paper_id AND l.topic = current.topic)"
    )
    row = await (await unit.execute(query, (since, since))).fetchone()
    assert row is not None  # a count has its row
    return row["n"]


async def collected_reactions(unit: ReadUnit, limit: int = 200) -> list[dict]:
    """Reactions that move nothing by themselves (decision 17), for the owner or an agent."""
    return await _all(
        unit,
        """
        SELECT r.paper_id, r.topic, r.value, r.reason, r.reacted_at, p.title
        FROM reactions r JOIN papers p ON p.id = r.paper_id
        LEFT JOIN topics t ON t.class = r.topic
        WHERE t.depth IS DISTINCT FROM 'focus'
           OR r.reason IS NULL OR r.reason NOT IN ('wrong_topic', 'great_find')
        ORDER BY r.reacted_at DESC LIMIT %s
        """,
        (limit,),
    )


async def runs(unit: ReadUnit, limit: int = 30) -> list[dict[str, Any]]:
    """The newest nightly runs, for the Nights table: jobs and other runs never push a night
    out of it. A night's `cost` is the day's spend outside jobs: its own run's, and
    `after_cost`, that of every other run outside a job started from its start until the next
    night's (the Briefs the worker writes once the night's batches have closed, say). Jobs show
    their own cost."""
    return await _all(
        unit,
        """
        SELECT n.id, n.kind, n.started_at, n.finished_at, n.cost + a.cost AS cost,
               a.cost AS after_cost, n.error, n.counts, n.fetched, n.inserted, n.updated
        FROM (SELECT *, lead(started_at) OVER (ORDER BY id) AS next_at FROM runs
              WHERE kind = 'nightly' ORDER BY id DESC LIMIT %s) n
        CROSS JOIN LATERAL (
            SELECT coalesce(sum(r.cost), 0) AS cost FROM runs r
            WHERE r.kind <> 'nightly' AND r.job_id IS NULL AND r.started_at >= n.started_at
              AND (n.next_at IS NULL OR r.started_at < n.next_at)) a
        ORDER BY n.id DESC
        """,
        (limit,),
    )


async def replay_recall(unit: ReadUnit) -> dict[str, Any] | None:
    """Recall per Focus topic from the latest replay (feedback.md, evaluation): the four numbers
    for each cohort and topic with their n, never pooled, and the run they come from; None
    before any replay. A replay still running, or cut short, counts what has concluded."""
    eval_run_id = await evaluation.latest(unit, "replay")
    if eval_run_id is None:
        return None
    run = await evaluation.get(unit, eval_run_id)
    return {
        "eval_run": eval_run_id,
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "cost": float(run["cost"]) if run["cost"] is not None else None,
        "recall": recall(await evaluation.outcomes(unit, eval_run_id)),
    }


async def reactions_by_topic(unit: ReadUnit, days: int = 30) -> list[dict]:
    """Thumbs per followed topic and reason over the trailing days (feedback.md, reactions:
    each reason points at a stage), for Overview's table, in the owner's topic order. A thumb
    without a reason has `reason` null."""
    return await _all(
        unit,
        """
        SELECT r.topic, r.reason, count(*) AS n FROM reactions r
        JOIN topics t ON t.class = r.topic
        WHERE r.reacted_at >= now() - make_interval(days => %s)
        GROUP BY t.position, r.topic, r.reason ORDER BY t.position, r.topic, r.reason
        """,
        (days,),
    )


async def count_misses(unit: ReadUnit) -> int:
    row = await (await unit.execute("SELECT count(*) AS n FROM misses")).fetchone()
    return row["n"]


async def misses(unit: ReadUnit, tree: Taxonomy | None) -> list[dict[str, Any]]:
    """Reported misses, newest first, each with what the stages concluded for the owner's topic
    on the paper's current text (feedback.md, misses): whether it has been classified and the
    classifier's probability for the topic's class, which of the classifier, the uncertain band
    and the keyword check made it a member (a classifier membership anywhere under the class
    counts, as it stands in the class, and reads `below` when it is under the class only; the
    band and the keyword check admit a paper to one Focus class only), and the current
    verdict: the Fit that stands by its depth's rule and each grader's opinion. Null where a
    stage has not run on this text."""
    # Each followed topic's class with every class under it, as (topic, class) pairs.
    pairs = [(t.slug, c) for t in (tree.topics if tree else []) for c in tree.subtree(t.slug)]
    return await _all(
        unit,
        """
        WITH under AS (SELECT * FROM unnest(%s::text[], %s::text[]) AS under (topic, class))
        SELECT m.id, m.paper_id, m.topic, m.reported_at, p.title,
               -- the classifier's own: a classification by hand is the owner's, not a stage
               EXISTS (SELECT 1 FROM classifications c WHERE c.paper_id = m.paper_id
                       AND c.paper_revision = p.revision AND c.model <> 'owner') AS classified,
               (SELECT (c.response->'probabilities'->>m.topic)::float FROM classifications c
                WHERE c.paper_id = m.paper_id AND c.paper_revision = p.revision
                  AND c.model <> 'owner'
                ORDER BY c.id DESC LIMIT 1) AS probability,
               -- 'below': a classifier membership in a class under the topic's, not in it
               (SELECT coalesce(array_agg(DISTINCT CASE WHEN x.class = m.topic THEN x.source
                                                        ELSE 'below' END), '{}')
                FROM memberships x
                WHERE x.paper_id = m.paper_id AND x.member AND x.paper_revision = p.revision
                  AND x.source IN ('classifier', 'uncertain', 'keyword')
                  AND (x.class = m.topic OR (x.source = 'classifier' AND x.class IN
                       (SELECT class FROM under WHERE under.topic = m.topic)))) AS sources,
               cur.opinions, cur.fit AS verdict_fit
        FROM misses m JOIN papers p ON p.id = m.paper_id
        LEFT JOIN LATERAL (SELECT v.fit, v.opinions FROM verdicts v
                           WHERE v.paper_id = m.paper_id AND v.topic = m.topic
                             AND v.paper_revision = p.revision
                           ORDER BY v.recorded_at DESC, v.id DESC LIMIT 1) cur ON true
        ORDER BY m.reported_at DESC, m.id DESC
        """,
        ([t for t, _ in pairs], [c for _, c in pairs]),
    )


async def class_members(unit: ReadUnit, tree: Taxonomy) -> dict[str, int]:
    """Papers standing in each class or any class under it (the memberships a page shows), by
    class slug; a class with none is left out."""
    pairs = [(c.slug, a) for c in tree.classes for a in with_ancestors(tree, {c.slug})]
    rows = await _all(
        unit,
        f"""
        SELECT map.target, count(DISTINCT m.paper_id) AS n
        FROM memberships m
        JOIN unnest(%s::text[], %s::text[]) AS map (class, target) ON map.class = m.class
        JOIN papers p ON p.id = m.paper_id AND NOT p.dropped
        WHERE {SHOWN_MEMBER}
        GROUP BY map.target
        """,
        ([c for c, _ in pairs], [a for _, a in pairs]),
    )
    return {r["target"]: r["n"] for r in rows}


async def topic_revisions(unit: ReadUnit) -> dict[str, int]:
    """The current revision of every followed topic, by class slug."""
    rows = await _all(unit, "SELECT class, revision FROM topics")
    return {r["class"]: r["revision"] for r in rows}


KEYWORD_DAYS = 90  # the window the topic editor counts keyword matches over


async def _keyword_hits(unit: ReadUnit, patterns: list[str], days: int) -> dict[str, list[str]]:
    """The papers published in the trailing days that each keyword pattern matches, as the
    keyword check matches them (core.classification.keyword_hit), not dismissed."""
    if not patterns:
        return {}
    rows = await _all(
        unit,
        "SELECT id, title, abstract FROM papers WHERE NOT dropped "
        "AND published >= now() - make_interval(days => %s) ORDER BY id",
        (days,),
    )

    # The regexes run over the whole window; a thread keeps the event loop free meanwhile.
    def hits() -> dict[str, list[str]]:
        return {
            k: [r["id"] for r in rows if keyword_hit(k, r["title"], r["abstract"])]
            for k in patterns
        }

    return await asyncio.to_thread(hits)


async def keyword_matches(
    unit: ReadUnit, patterns: list[str], days: int = KEYWORD_DAYS
) -> dict[str, int]:
    """How many papers published in the trailing days each keyword pattern matches."""
    return {k: len(ids) for k, ids in (await _keyword_hits(unit, patterns, days)).items()}


async def keyword_papers(
    unit: ReadUnit, patterns: list[str], days: int = KEYWORD_DAYS
) -> list[str]:
    """The papers published in the trailing days that any keyword pattern matches: the papers
    the editor's keyword counts count."""
    found = await _keyword_hits(unit, patterns, days)
    return sorted({p for ids in found.values() for p in ids})
