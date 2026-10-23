"""Digests and their papers (data.md: `digests`, `digest_papers`), what a digest is to cover,
and the reads behind the Digest tab.

What is due is not stored: it is found from the database when a digest is written, so every way
a paper becomes or stops being eligible (a grading, a re-check, the owner's word, a class
label, an edit of the followed topics) is seen without a hook of its own. A paper is due for a
topic while it is eligible there (`_ELIGIBLE`), was announced within `DUE_WINDOW` of the
digest's day, and is in no published digest of the topic that clears it: a daily digest for
Focus and Area topics, a weekly one for Field topics. An old paper back with a new arXiv version
is due as updated by rules of its own (`due_updated`), found the same way."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch import events
from litwatch.core import Depth, Grade, Status, Taxonomy, Topic
from litwatch.core.digests import (
    DUE_WINDOW,
    PUBLISHED,
    DigestPeriod,
    DigestRole,
    DigestState,
    listing_day,
)
from litwatch.core.fulltext import AWAITING_TOO_LONG
from litwatch.core.grading import GRADES, NO_WEIGHT
from litwatch.core.rules import REACTION_WORDS
from litwatch.store.analyses import CURRENT_GIST
from litwatch.store.reading import SCOPE, STANDING, _rank, _scope

_PUBLISHED = "(" + ", ".join(f"'{s.value}'" for s in PUBLISHED) + ")"


@dataclass(frozen=True)
class Due:
    """A paper a digest is to cover, as it stands now on the digest's topic: its effective Fit
    and grade there (its own, or a followed descendant's, `graded_on`), the page's rarity, and
    what the writer reads of it."""

    paper_id: str
    revision: int
    title: str
    announced: date | None
    gist: str | None
    fit: str | None
    grade: str | None
    rarity: str
    graded_on: str | None
    fit_reason: str | None
    brief: str | None
    abstract: str | None = None  # a Focus paper's, which the writer reads when it has no Brief
    version: int | None = None  # an updated paper's: the arXiv version it came back with


# The covering digests of topic %(topic)s that clear paper `p` for %(period)s. An updated row
# is no cover: it is a role of its own (`_SHOWN_UPDATED`).
_COVERED = f"""EXISTS (
    SELECT 1 FROM digest_papers dp JOIN digests d ON d.id = dp.digest_id
    WHERE dp.paper_id = p.id AND d.topic = %(topic)s AND d.period = %(period)s
      AND d.state IN {_PUBLISHED} AND dp.role <> 'updated')"""

# A published digest of topic %(topic)s showed paper `p` as new, of either period: it never
# comes back as updated (digests.md, updated papers).
_SHOWN_NEW = f"""EXISTS (
    SELECT 1 FROM digest_papers dp JOIN digests d ON d.id = dp.digest_id
    WHERE dp.paper_id = p.id AND d.topic = %(topic)s AND d.state IN {_PUBLISHED}
      AND dp.role <> 'updated')"""

# A published digest of topic %(topic)s of %(period)s listed paper `p` as updated, at any
# version: once per period.
_SHOWN_UPDATED = f"""EXISTS (
    SELECT 1 FROM digest_papers dp JOIN digests d ON d.id = dp.digest_id
    WHERE dp.paper_id = p.id AND d.topic = %(topic)s AND d.period = %(period)s
      AND d.state IN {_PUBLISHED} AND dp.role = 'updated')"""

# Paper `p`'s newest version (`nv`) when it is after the first and its submission time is
# known: the version an old paper comes back with as updated, and when it was submitted (NULL
# without one, which only a weekly's recap of its dailies admits).
_NEWEST = """
    LEFT JOIN LATERAL (
        SELECT (e->>'date')::timestamptz AS submitted
        FROM jsonb_array_elements(CASE WHEN jsonb_typeof(p.version_history) = 'array'
                                       THEN p.version_history ELSE '[]' END) e
        WHERE p.version > 1 AND e->>'version' = p.version::text
          AND pg_input_is_valid(e->>'date', 'timestamptz')
        LIMIT 1) nv ON true"""

# What an updated paper's row adds: the version it came back with, and when that was submitted.
_VERSION = ", p.version, nv.submitted"
# The (Fit, Weight) pairs the pipeline grades must read (`core.grading.GRADES`).
_MUST_READ = (
    "("
    + ", ".join(
        f"('{f.value}', '{w.value}')"
        for f, row in GRADES.items()
        for w, g in row.items()
        if g == Grade.MUST_READ
    )
    + ")"
)
# Each grade's place, most inclusive first: an updated list's order before rarity.
_GRADES = "ARRAY[" + ", ".join(f"'{g.value}'" for g in Grade) + "]::text[]"

# A Brief or Weight re-check of paper `p`'s current text still to finish: open and not terminal
# (pipeline.md, section 6), and no pass for that text done. An Area paper graded must read from
# its abstract waits for it (digests.md, which papers a digest covers).
_RECHECKING = """(
    EXISTS (SELECT 1 FROM fulltext_passes f
            WHERE f.paper_id = p.id AND f.paper_revision = p.revision AND f.kind <> 'deep'
              AND (f.state IN ('ready', 'running')
                   OR (f.state = 'awaiting_text' AND f.queued_at >= now() - %(long)s)
                   OR (f.state = 'failed' AND f.failed_runs < %(runs)s)))
    AND NOT EXISTS (SELECT 1 FROM fulltext_passes f
                    WHERE f.paper_id = p.id AND f.paper_revision = p.revision
                      AND f.kind <> 'deep' AND f.state = 'done'))"""

# Grading has finished for paper `p` under the topic: its current text's grading pass is done,
# and no grading for an active topic in the subtree is failed and still to be tried again (a
# paused topic grades nothing, so its failures wait for the owner).
_GRADED = """
    EXISTS (SELECT 1 FROM grading_passes g
            WHERE g.paper_id = p.id AND g.paper_revision = p.revision)
    AND NOT EXISTS (SELECT 1 FROM step_failures f
                    WHERE f.paper_id = p.id AND f.step = 'grading'
                      AND f.paper_revision = p.revision AND f.failed_runs < %(runs)s
                      AND f.topic = ANY(%(active)s))"""

_WRITER_READS = f"""
    {CURRENT_GIST} AS gist,
    (SELECT b.text FROM briefs b WHERE b.paper_id = p.id AND b.paper_revision = p.revision
     ORDER BY b.pass_id DESC LIMIT 1) AS brief"""


def _word(paper: str, topic: str) -> str:
    """Whether the owner's word on `paper` for `topic` is in force (`core.rules.owners_word`):
    a label other than unsure, or on a Focus topic a reaction that carries a word."""
    words = ", ".join(f"'{r.value}'" for r in REACTION_WORDS)
    return f"""(
    EXISTS (SELECT 1 FROM labels l
            WHERE l.paper_id = {paper} AND l.topic = {topic} AND l.value <> 'unsure')
    OR EXISTS (SELECT 1 FROM reactions r JOIN topics t ON t.class = r.topic
               WHERE r.paper_id = {paper} AND r.topic = {topic} AND t.depth = 'focus'
                 AND r.reason IN ({words})))"""


def _fit(topic: str, older: bool = False) -> str:
    """The Fit of the latest verdict on paper `p` for `topic`; with `older`, on an earlier
    revision."""
    before = "AND v.paper_revision < p.revision" if older else ""
    return f"""(
    SELECT v.fit FROM verdicts v WHERE v.paper_id = p.id AND v.topic = {topic} {before}
    ORDER BY v.paper_revision DESC, v.recorded_at DESC, v.id DESC LIMIT 1)"""


def _member(narrow: str) -> str:
    """Paper `p` is a member of the topic's subtree by the pipeline, not by the owner's word (a
    class label, a reported miss, which grading follows too): by classification, or by the
    uncertain band or the keyword check where `narrow` holds; and not taken out of that class by
    a class label."""
    return f"""
    EXISTS (SELECT 1 FROM memberships m
            WHERE m.paper_id = p.id AND m.class = ANY(%(map_classes)s) AND m.member
              AND m.source IN ('classifier', 'uncertain', 'keyword')
              AND (m.source = 'classifier' OR {narrow})
              AND NOT EXISTS (SELECT 1 FROM memberships o
                              WHERE o.paper_id = m.paper_id AND o.class = m.class
                                AND o.source = 'owner' AND NOT o.member))"""


def _held(topic: str) -> str:
    """Grading follows paper `p` to `topic` from an older text (`held`, pipeline.md, section 8):
    its latest verdict there on an earlier revision had a Fit above none, and the owner's word
    has no say on that class: no class label of the owner's put it in or took it out, and no
    reported miss put it in."""
    return f"""(
    {_fit(topic, older=True)} <> 'none'
    AND NOT EXISTS (SELECT 1 FROM memberships o
                    WHERE o.paper_id = p.id AND o.class = {topic}
                      AND (o.source = 'owner' OR (o.source = 'miss' AND o.member))))"""


# A Focus topic's papers reach it by the pipeline: a member of its class, or held there.
_FOCUS_LED = f"""({_member("m.class = %(topic)s")} OR {_held("%(topic)s")})"""

# An Area's or Field's: a member of its subtree, an uncertain-band or keyword member of a Focus
# class there counting once graded central or related on it, so it is not admitted ungraded;
# or held on a topic in the subtree and graded central or related there now.
_SUBTREE_LED = f"""(
    {_member(f"{_fit('m.class')} IN ('central', 'related')")}
    OR EXISTS (SELECT 1 FROM unnest(%(up_below)s::text[]) u (topic)
               WHERE {_held("u.topic")} AND {_fit("u.topic")} IN ('central', 'related')))"""


def _order(updated: bool, grade: str, rarity: str) -> str:
    """A digest's list, rarest first; an updated list by grade, then rarity, then the paper
    first out most recently (a Field's must reads are alike in grade and rarity)."""
    if updated:
        return f"array_position({_GRADES}, {grade}), {_rank(rarity)} DESC, p.announced DESC, p.id"
    return f"{_rank(rarity)} DESC, p.announced, p.id"


def _focus(which: str, updated: bool = False) -> str:
    """A Focus topic's eligible papers: graded there, at effective Fit central or related, and
    at a verdict's Fit on the current text that was too (a rewritten paper waits for its
    grading), so the owner's word, which the owner already knows, never
    makes a paper due, and led there by the pipeline (`_FOCUS_LED`). Its rarity is a mixed
    list's, as in every digest (pipeline.md, section 5). With `updated`, each with its newest
    version (`_NEWEST`), which `which` may read as `nv`."""
    rarity = "coalesce(r.level, g.rarity)"
    return f"""
    SELECT p.id AS paper_id, p.revision, p.title, p.announced, g.fit, g.grade,
           {rarity} AS rarity, g.topic AS graded_on, v.reason AS fit_reason,
           p.abstract, {_WRITER_READS}{_VERSION if updated else ""}
    FROM papers p
    JOIN effective_grades g ON g.paper_id = p.id AND g.topic = %(topic)s
    LEFT JOIN rarities r ON r.paper_id = p.id
    JOIN LATERAL (SELECT v.fit, v.reason FROM verdicts v
                  WHERE v.paper_id = p.id AND v.topic = %(topic)s
                    AND v.paper_revision = p.revision
                  ORDER BY v.recorded_at DESC, v.id DESC LIMIT 1) v ON true
    {_NEWEST if updated else ""}
    WHERE p.dismissed_at IS NULL AND g.fit IN ('central', 'related')
      AND v.fit IN ('central', 'related') AND {which} AND {_FOCUS_LED}
    ORDER BY {_order(updated, "g.grade", rarity)}
    """


def _subtree(which: str, gates: str, updated: bool = False) -> str:
    """An Area or Field topic's papers: its subtree's papers by the pipeline (`_SUBTREE_LED`),
    at its page's standing (its rarity a mixed list's), less those the owner's word sets below
    related where they are graded. With `updated`, as in `_focus`."""
    rarity = "coalesce(r.level, s.rarity, 'common')"
    return f"""
    WITH {SCOPE}{STANDING}
    SELECT p.id AS paper_id, p.revision, p.title, p.announced, g.fit, s.grade,
           {rarity} AS rarity, g.topic AS graded_on,
           v.reason AS fit_reason, {_WRITER_READS}{_VERSION if updated else ""}
    FROM standing s JOIN papers p ON p.id = s.paper_id
    LEFT JOIN rarities r ON r.paper_id = p.id
    LEFT JOIN effective_grades g
         ON g.paper_id = p.id AND s.grade IS NOT NULL AND g.topic = coalesce(s.via, s.topic)
    LEFT JOIN LATERAL (SELECT v.reason FROM verdicts v
                       WHERE v.paper_id = p.id AND v.topic = g.topic
                       ORDER BY v.paper_revision DESC, v.recorded_at DESC, v.id DESC
                       LIMIT 1) v ON true
    {_NEWEST if updated else ""}
    WHERE p.dismissed_at IS NULL AND {which} AND {_SUBTREE_LED}
      AND (g.fit IN ('adjacent', 'none') AND {_word("p.id", "g.topic")}) IS NOT TRUE {gates}
    ORDER BY {_order(updated, "s.grade", rarity)}
    """


def _rows(rows: list[dict[str, Any]]) -> list[Due]:
    return [Due(**r) for r in rows]


def _params(tree: Taxonomy, topic: Topic, runs: int) -> dict[str, Any]:
    return _scope(tree, [topic.slug]) | {
        "topic": topic.slug,
        "active": [t.slug for t in tree.under(topic.slug) if t.status == Status.ACTIVE],
        "runs": runs,
        "long": AWAITING_TOO_LONG,
    }


async def due(
    conn: psycopg.AsyncConnection,
    tree: Taxonomy,
    topic: Topic,
    period: DigestPeriod,
    last: date,
    runs: int,
) -> list[Due]:
    """The papers due for the topic's digest of `period` whose listing is on or before `last`
    (the digest's day, or its week's Sunday), rarest first: for Focus, those graded there at
    Fit central or related; for Area and Field, the subtree's members that grading has finished
    for, and for Area not one graded must read from its abstract while its Weight re-check is
    still to finish (`_RECHECKING`). `runs` is how many failed runs make a step terminal."""
    which = f"p.announced BETWEEN %(first)s AND %(last)s AND NOT {_COVERED}"
    params = _params(tree, topic, runs) | {
        "period": period.value,
        "first": last - DUE_WINDOW,
        "last": last,
    }
    if topic.depth == Depth.FOCUS:
        query = _focus(which)
    else:
        gates = f"AND {_GRADED}"
        if topic.depth == Depth.AREA:
            gates += f" AND NOT (s.grade IS NOT DISTINCT FROM 'must_read' AND {_RECHECKING})"
        query = _subtree(which, gates)
    return _rows(await (await conn.execute(query, params)).fetchall())


async def of_week(
    conn: psycopg.AsyncConnection, tree: Taxonomy, topic: Topic, monday: date
) -> list[Due]:
    """What the weekly digest of a topic with daily digests covers, rarest first: the papers
    announced by the week's Sunday that its published daily digests covered, up to the
    Monday after (written the same night, before the weekly), and no weekly digest of it has,
    as they stand now and still eligible. A paper that became due late in the week is in it;
    one whose daily digest failed until after it is in the next."""
    sunday = monday + timedelta(days=6)
    which = f"""p.announced BETWEEN %(first)s AND %(sunday)s AND NOT {_COVERED} AND EXISTS (
        SELECT 1 FROM digest_papers dp JOIN digests d ON d.id = dp.digest_id
        WHERE dp.paper_id = p.id AND d.topic = %(topic)s AND d.period = 'day'
          AND d.state = 'written' AND d.day <= %(after)s AND dp.role <> 'updated')"""
    params = _params(tree, topic, 0) | {
        "period": DigestPeriod.WEEK.value,
        "first": sunday - DUE_WINDOW,
        "sunday": sunday,
        "after": sunday + timedelta(days=1),
    }
    query = _focus(which) if topic.depth == Depth.FOCUS else _subtree(which, "")
    return _rows(await (await conn.execute(query, params)).fetchall())


async def due_updated(
    conn: psycopg.AsyncConnection,
    tree: Taxonomy,
    topic: Topic,
    period: DigestPeriod,
    last: date,
    runs: int,
    week: date | None = None,
) -> list[Due]:
    """The old papers due for the topic's digest of `period` as updated (digests.md, updated
    papers), by grade, then rarity, then newest first out, each with its newest version: one
    whose listing day is within `DUE_WINDOW` before `last` (the digest's day, or its week's
    Sunday) and more than `DUE_WINDOW` after the paper's first; eligible there now at a higher
    bar than a new paper (for Focus, effective Fit central or related; for Area and Field,
    graded must read, an Area's after its Weight re-check, as a pick, and by the pipeline on its
    current text, so neither an older text's grade nor the owner's word makes it due); never
    shown as new by a published digest of the topic, nor as updated by one of `period`. A Focus
    or Area weekly gives its `week`'s Monday: it takes the versions listed that week, and
    recaps, at the version they listed, the papers its published dailies listed as updated, up
    to the Monday after, still eligible, whatever version has come since."""
    first = last - DUE_WINDOW
    recapped = {} if week is None else await _listed_updated(conn, topic.slug, week)
    # A listing day comes one to four days after its submission's US Eastern date
    # (`listing_day`): these bounds hold every submission that can list in the window.
    which = f"""((nv.submitted >= %(since)s AND nv.submitted < %(until)s
                  AND p.announced < %(old)s) OR p.id = ANY(%(recapped)s))
        AND NOT {_SHOWN_NEW} AND NOT {_SHOWN_UPDATED}"""
    params = _params(tree, topic, runs) | {
        "period": period.value,
        "since": datetime.combine(first - timedelta(days=5), time(), UTC),
        "until": datetime.combine(last + timedelta(days=1), time(), UTC),
        "old": first,
        "recapped": list(recapped),
    }
    if topic.depth == Depth.FOCUS:
        query = _focus(which, updated=True)
    else:
        # The grade shown is must read, and so is the pipeline's where it is graded: the latest
        # verdict on the current text at the paper's Weight, not an older text's grade or the
        # owner's word.
        gates = f"""AND {_GRADED} AND s.grade = 'must_read' AND ((
            SELECT v.fit FROM verdicts v
            WHERE v.paper_id = p.id AND v.topic = g.topic AND v.paper_revision = p.revision
            ORDER BY v.recorded_at DESC, v.id DESC LIMIT 1),
            coalesce(r.weight, '{NO_WEIGHT.value}')) IN {_MUST_READ}"""
        if topic.depth == Depth.AREA:
            gates += f" AND NOT {_RECHECKING}"
        query = _subtree(which, gates, updated=True)
    found = []
    for row in await (await conn.execute(query, params)).fetchall():
        submitted = row.pop("submitted")
        if row["paper_id"] in recapped:
            found.append(Due(**row | {"version": recapped[row["paper_id"]]}))
            continue
        day = listing_day(submitted) if submitted is not None else None
        if day is None or not first <= day <= last or row["announced"] >= day - DUE_WINDOW:
            continue
        if week is None or day >= week:
            found.append(Due(**row))
    return found


async def _listed_updated(
    conn: psycopg.AsyncConnection, topic: str, monday: date
) -> dict[str, int]:
    """The papers the topic's published daily digests listed as updated, each with the version
    listed, up to the Monday after the week from `monday` (written the same night, before the
    weekly), less a version of that Monday's own listing: it is the next week's."""
    after = monday + timedelta(days=7)
    rows = await (
        await conn.execute(
            f"""SELECT dp.paper_id, dp.version, (
                    SELECT (e->>'date')::timestamptz
                    FROM papers p, jsonb_array_elements(
                        CASE WHEN jsonb_typeof(p.version_history) = 'array'
                             THEN p.version_history ELSE '[]' END) e
                    WHERE p.id = dp.paper_id AND (e->>'version')::int = dp.version
                    LIMIT 1) AS submitted
                FROM digest_papers dp JOIN digests d ON d.id = dp.digest_id
                WHERE d.topic = %s AND d.period = 'day' AND d.state IN {_PUBLISHED}
                  AND d.day <= %s AND dp.role = 'updated'""",
            (topic, after),
        )
    ).fetchall()
    found: dict[str, int] = {}
    for r in rows:
        if r["submitted"] is not None and listing_day(r["submitted"]) >= after:
            continue
        found[r["paper_id"]] = max(r["version"], found.get(r["paper_id"], 0))
    return found


async def earlier_central(
    conn: psycopg.AsyncConnection, topic: str, before: date, limit: int = 20
) -> list[Due]:
    """The papers the topic's digests listed at Fit central before `before`, newest first, each
    as it was last shown: what a Focus weekly digest may bring back."""
    rows = await (
        await conn.execute(
            """
            SELECT paper_id, revision, title, announced, gist, fit, grade, rarity, graded_on,
                   NULL AS fit_reason, NULL AS brief FROM (
                SELECT DISTINCT ON (dp.paper_id) dp.paper_id, dp.paper_revision AS revision,
                       dp.title, p.announced, dp.gist, dp.fit, dp.grade, dp.rarity, dp.graded_on,
                       d.day
                FROM digest_papers dp JOIN digests d ON d.id = dp.digest_id
                JOIN papers p ON p.id = dp.paper_id
                WHERE d.topic = %s AND d.state = 'written' AND d.day < %s AND dp.fit = 'central'
                  AND dp.role IN ('pick', 'listed')
                ORDER BY dp.paper_id, d.day DESC) x
            ORDER BY day DESC, paper_id LIMIT %s
            """,
            (topic, before, limit),
        )
    ).fetchall()
    return _rows(rows)


async def as_shown(conn: psycopg.AsyncConnection, digest_id: int, focus: bool) -> list[Due]:
    """A published digest's papers the writer was given (not its updated ones), with what it
    froze of each (title, Gist, Fit, rarity), in a
    writer's order (rarest first), each with the latest Brief and Fit reason of the revision
    shown, and for a `focus` topic its abstract when that revision is still current: the
    writer's input rebuilt for an evaluation's request. It is the input the writer had as far as
    no Brief was written and no grading recorded for that revision since."""
    rows = await (
        await conn.execute(
            f"""
            SELECT dp.paper_id, dp.paper_revision AS revision, dp.title, p.announced, dp.gist,
                   dp.fit, dp.grade, dp.rarity, dp.graded_on,
                   (SELECT v.reason FROM verdicts v
                    WHERE v.paper_id = dp.paper_id AND v.topic = dp.graded_on
                      AND v.paper_revision = dp.paper_revision
                    ORDER BY v.recorded_at DESC, v.id DESC LIMIT 1) AS fit_reason,
                   (SELECT b.text FROM briefs b
                    WHERE b.paper_id = dp.paper_id AND b.paper_revision = dp.paper_revision
                    ORDER BY b.pass_id DESC LIMIT 1) AS brief,
                   CASE WHEN %(focus)s AND p.revision = dp.paper_revision THEN p.abstract END
                       AS abstract
            FROM digest_papers dp JOIN papers p ON p.id = dp.paper_id
            WHERE dp.digest_id = %(digest)s AND dp.role <> 'updated'
            ORDER BY {_rank("dp.rarity")} DESC, p.announced, dp.paper_id
            """,
            {"digest": digest_id, "focus": focus},
        )
    ).fetchall()
    return _rows(rows)


async def daily_texts(conn: psycopg.AsyncConnection, topic: str, monday: date) -> list[dict]:
    """The texts of the topic's daily digests written in the week from `monday`."""
    return await (
        await conn.execute(
            "SELECT day, text FROM digests WHERE topic = %s AND period = 'day' "
            "AND state = 'written' AND day BETWEEN %s AND %s ORDER BY day",
            (topic, monday, monday + timedelta(days=6)),
        )
    ).fetchall()


async def released(conn: psycopg.AsyncConnection, day: date) -> bool:
    """Whether arXiv listed papers on `day`: a paper litwatch holds is announced on it, dismissed
    or not (dismissing stops processing; it does not undo the listing)."""
    row = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM papers WHERE announced = %s) AS released",
            (day,),
        )
    ).fetchone()
    return row["released"]


async def state_of(
    conn: psycopg.AsyncConnection, topic: str, period: DigestPeriod, day: date
) -> DigestState | None:
    row = await (
        await conn.execute(
            "SELECT state FROM digests WHERE topic = %s AND period = %s AND day = %s",
            (topic, period.value, day),
        )
    ).fetchone()
    return DigestState(row["state"]) if row else None


# -- recording -------------------------------------------------------------------------------------


@dataclass
class Shown:
    """One paper as a digest shows it, frozen with it."""

    due: Due
    role: DigestRole
    why: str | None = None


@dataclass
class Outcome:
    """What writing one digest came to: published (written or nothing new), or failed."""

    topic: str
    period: DigestPeriod
    day: date
    state: DigestState
    gist: str | None = None
    text: str | None = None
    papers: list[Shown] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    best_rarity: str | None = None
    error: str | None = None
    # The writer model that wrote it or, failed, the one its attempt started from (models.md, roles)
    model: str | None = None


async def record(conn: psycopg.AsyncConnection, o: Outcome, run_id: int | None) -> int | None:
    """Store the outcome, in place of a failed digest of the same topic and period; a published
    one stays as it is (None). Publishing emits `digest.written` with it. Returns the digest's
    id."""
    async with conn.transaction():
        row = await (
            await conn.execute(
                f"""
                INSERT INTO digests (topic, period, day, state, gist, text, counts, best_rarity,
                                     error, model, run_id)
                VALUES (%(topic)s, %(period)s, %(day)s, %(state)s, %(gist)s, %(text)s,
                        %(counts)s, %(best)s, %(error)s, %(model)s, %(run)s)
                ON CONFLICT (topic, period, day) DO UPDATE SET
                    state = EXCLUDED.state, gist = EXCLUDED.gist, text = EXCLUDED.text,
                    counts = EXCLUDED.counts, best_rarity = EXCLUDED.best_rarity,
                    error = EXCLUDED.error, model = EXCLUDED.model, run_id = EXCLUDED.run_id,
                    written_at = clock_timestamp(),
                    -- failed again, it stays as the owner saw it; published, there is none to see
                    acknowledged_at = CASE WHEN EXCLUDED.state = 'failed'
                                           THEN digests.acknowledged_at END
                WHERE digests.state NOT IN {_PUBLISHED}
                RETURNING id
                """,
                {
                    "topic": o.topic,
                    "period": o.period.value,
                    "day": o.day,
                    "state": o.state.value,
                    "gist": o.gist,
                    "text": o.text,
                    "counts": Jsonb(o.counts),
                    "best": o.best_rarity,
                    "error": o.error[:2000] if o.error else None,
                    "model": o.model,
                    "run": run_id,
                },
            )
        ).fetchone()
        if row is None:
            return None
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO digest_papers (digest_id, paper_id, role, position, why, "
                "paper_revision, title, gist, fit, grade, rarity, graded_on, version) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        row["id"],
                        s.due.paper_id,
                        s.role.value,
                        i,
                        s.why,
                        s.due.revision,
                        s.due.title,
                        s.due.gist,
                        s.due.fit,
                        s.due.grade,
                        s.due.rarity,
                        s.due.graded_on,
                        s.due.version,
                    )
                    for i, s in enumerate(o.papers)
                ],
            )
        if o.state in PUBLISHED:
            await events.emit(
                conn,
                events.DIGEST_WRITTEN,
                digest=row["id"],
                topic=o.topic,
                period=o.period.value,
                day=o.day.isoformat(),
                state=o.state.value,
            )
        return row["id"]


# A failed digest of an active topic: written again by the next run, and on Quality meanwhile.
# A paused or unfollowed topic's waits for nothing.
_FAILED = (
    "FROM digests d JOIN topics t ON t.class = d.topic AND t.status = 'active' "
    "WHERE d.state = 'failed'"
)


async def failed(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    """The failed digests of active topics, oldest first, the days before the weeks, with when
    the owner acknowledged them."""
    return await (
        await conn.execute(
            "SELECT d.id, d.topic, d.period, d.day, d.error, d.written_at, d.acknowledged_at "
            f"{_FAILED} ORDER BY d.day, d.period, d.topic"
        )
    ).fetchall()


async def failed_writer(conn: psycopg.AsyncConnection, topic: str) -> str | None:
    """The writer model the topic's latest failed digest attempt started from, while no digest
    of the topic has been published since: the next attempt starts one model further down
    `writer`'s list (models.md, roles). None when its latest digest is published, or it has
    none."""
    row = await (
        await conn.execute(
            "SELECT state, model FROM digests WHERE topic = %s "
            "ORDER BY written_at DESC, id DESC LIMIT 1",
            (topic,),
        )
    ).fetchone()
    return row["model"] if row and row["state"] == DigestState.FAILED else None


async def count_failed(conn: psycopg.AsyncConnection, *, unacknowledged: bool = False) -> int:
    """How many digests failed; with `unacknowledged`, only those the owner has not acknowledged."""
    seen = " AND d.acknowledged_at IS NULL" if unacknowledged else ""
    row = await (await conn.execute(f"SELECT count(*) AS n {_FAILED}{seen}")).fetchone()
    return row["n"]


async def acknowledge(conn: psycopg.AsyncConnection, ids: list[int]) -> list[int]:
    """The owner's Acknowledge on the named failed digests, of active topics (a failed digest
    stays the same failure until it is published); one marked already keeps its time. Returns
    the ids marked."""
    if not ids:
        return []
    rows = await (
        await conn.execute(
            "UPDATE digests d SET acknowledged_at = coalesce(d.acknowledged_at, now()) "
            "FROM topics t "
            "WHERE d.id = ANY(%s) AND t.class = d.topic AND t.status = 'active' "
            "AND d.state = 'failed' RETURNING d.id",
            (ids,),
        )
    ).fetchall()
    return [r["id"] for r in rows]


async def get(conn: psycopg.AsyncConnection, digest_id: int) -> dict[str, Any] | None:
    return await (
        await conn.execute("SELECT * FROM digests WHERE id = %s", (digest_id,))
    ).fetchone()


async def waiting_passes(conn: psycopg.AsyncConnection, since: datetime) -> int:
    """The Briefs and Weight re-checks queued since `since` (by a Focus or an Area topic) that a
    digest waits for: still to run on their paper's current text, the paper not dismissed. One
    that failed in this run, or is terminal, waits no longer; nor does a re-run's, which only
    its job works."""
    row = await (
        await conn.execute(
            "SELECT count(*) AS n FROM fulltext_passes f JOIN papers p ON p.id = f.paper_id "
            "WHERE f.kind <> 'deep' AND f.reason IN ('focus', 'area') AND f.queued_at >= %s "
            "AND f.paper_revision = p.revision AND p.dismissed_at IS NULL AND f.job_id IS NULL "
            "AND f.state IN ('awaiting_text', 'ready', 'running')",
            (since,),
        )
    ).fetchone()
    return row["n"]


# -- the Digest tab ------------------------------------------------------------------------------

# Up to this many of a digest's papers show on its card: its picks, else its central papers.
CARD_PAPERS = 3


async def digests_on(
    conn: psycopg.AsyncConnection,
    period: DigestPeriod,
    first: date,
    last: date,
    topic: str | None = None,
) -> list[dict[str, Any]]:
    """The digests of `period` from `first` to `last`, of `topic` alone when given, newest
    first."""
    return await (
        await conn.execute(
            "SELECT id, topic, period, day, state, gist, counts, best_rarity FROM digests "
            "WHERE period = %(period)s AND day BETWEEN %(first)s AND %(last)s "
            "AND (%(topic)s::text IS NULL OR topic = %(topic)s) ORDER BY day DESC, topic",
            {"period": period.value, "first": first, "last": last, "topic": topic},
        )
    ).fetchall()


# A digest's papers as it shows them: frozen, with whether the owner has since set each aside:
# the owner's word is in force where its grade was shown and puts it below relevant.
_SHOWN = f"""
    SELECT dp.digest_id, dp.paper_id, dp.role, dp.position, dp.why, dp.paper_revision, dp.gist,
           dp.fit, dp.grade, dp.rarity, dp.graded_on, dp.title, dp.version, p.announced,
           p.published,
           (g.grade NOT IN ('must_read', 'relevant')
            AND {_word("dp.paper_id", "dp.graded_on")}) IS TRUE AS set_aside
    FROM digest_papers dp JOIN papers p ON p.id = dp.paper_id
    LEFT JOIN effective_grades g ON g.paper_id = dp.paper_id AND g.topic = dp.graded_on
"""


async def card_papers(conn: psycopg.AsyncConnection, ids: list[int]) -> list[dict[str, Any]]:
    """The papers each digest's card shows (`CARD_PAPERS`): its picks, else its central papers,
    in the digest's order."""
    return await (
        await conn.execute(
            f"""
            SELECT * FROM (
                SELECT x.*, row_number() OVER (PARTITION BY x.digest_id
                                               ORDER BY x.position) AS n
                FROM ({_SHOWN} WHERE dp.digest_id = ANY(%s)) x
                WHERE x.role = 'pick' OR NOT EXISTS (
                          SELECT 1 FROM digest_papers k
                          WHERE k.digest_id = x.digest_id AND k.role = 'pick')
                      AND x.role = 'listed' AND x.fit = 'central') y
            WHERE n <= %s ORDER BY digest_id, position
            """,
            (ids, CARD_PAPERS),
        )
    ).fetchall()


async def papers_of(conn: psycopg.AsyncConnection, digest_id: int) -> list[dict[str, Any]]:
    """A digest's papers in its order (`_SHOWN`)."""
    return await (
        await conn.execute(f"{_SHOWN} WHERE dp.digest_id = %s ORDER BY dp.position", (digest_id,))
    ).fetchall()


async def released_days(conn: psycopg.AsyncConnection, first: date, last: date) -> set[date]:
    """The days from `first` to `last` on which arXiv listed papers litwatch holds, dismissed or
    not."""
    rows = await (
        await conn.execute(
            "SELECT DISTINCT announced FROM papers WHERE announced BETWEEN %s AND %s",
            (first, last),
        )
    ).fetchall()
    return {r["announced"] for r in rows}


async def step(
    conn: psycopg.AsyncConnection,
    period: DigestPeriod,
    day: date,
    later: bool,
    topics: list[str],
) -> date | None:
    """The nearest day before `day` (or after, `later`) with digests of `period` of `topics`
    (those shown): where the Day or Week view steps, past the days without."""
    op, order = (">", "ASC") if later else ("<", "DESC")
    row = await (
        await conn.execute(
            f"SELECT day FROM digests WHERE period = %s AND day {op} %s AND topic = ANY(%s) "
            f"ORDER BY day {order} LIMIT 1",
            (period.value, day, topics),
        )
    ).fetchone()
    return row["day"] if row else None


async def first_day(conn: psycopg.AsyncConnection, topics: list[str]) -> date | None:
    """The day of the oldest digest of `topics` (those shown): where History stops."""
    row = await (
        await conn.execute("SELECT min(day) AS day FROM digests WHERE topic = ANY(%s)", (topics,))
    ).fetchone()
    return row["day"]


async def strips(
    conn: psycopg.AsyncConnection, topics: list[str], period: DigestPeriod, limit: int
) -> list[dict[str, Any]]:
    """Each topic's latest `limit` digests of `period`, oldest first: its digest strip."""
    return await (
        await conn.execute(
            """
            SELECT id, topic, day, state, best_rarity FROM (
                SELECT d.*, row_number() OVER (PARTITION BY d.topic ORDER BY d.day DESC) AS n
                FROM digests d WHERE d.topic = ANY(%s) AND d.period = %s) x
            WHERE n <= %s ORDER BY topic, day
            """,
            (topics, period.value, limit),
        )
    ).fetchall()


async def written(conn: psycopg.AsyncConnection, first: date, last: date) -> list[dict[str, Any]]:
    """The written digests from `first` to `last` (a weekly one by its Monday), with the
    titles of the papers the writer was given (not its updated papers): what the voice check
    reads."""
    return await (
        await conn.execute(
            """
            SELECT d.id, d.topic, d.period, d.day, d.gist, d.text, d.counts,
                   array(SELECT dp.title FROM digest_papers dp
                         WHERE dp.digest_id = d.id AND dp.role <> 'updated') AS titles
            FROM digests d WHERE d.state = 'written' AND d.day BETWEEN %s AND %s
            ORDER BY d.day, d.period, d.topic
            """,
            (first, last),
        )
    ).fetchall()
