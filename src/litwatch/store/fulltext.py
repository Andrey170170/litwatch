"""The full-text queue and what its passes wrote (data.md: `fulltext_passes`, `briefs`, and the
full-text rows of `weights` and `gists`)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from litwatch.core import Paper, TaxonomyClass, Weight
from litwatch.core.digests import PUBLISHED
from litwatch.core.fulltext import (
    AUTOMATIC_PRIORITY,
    AWAITING_TOO_LONG,
    REQUEST_PRIORITY,
    REQUESTED,
    TEXT_PRIORITY,
    TEXT_REQUEST_PRIORITY,
    UNSTARTED,
    DeepPass,
    PassKind,
    PassState,
    TextPass,
    TextRead,
)
from litwatch.store.papers import COLUMNS, row_to_paper
from litwatch.store.taxonomy import lock_shared, row_to_class

# Whether text pass `f` read paper `p`'s text for its latest arXiv update, or none is known: what
# keeps a done pass current while the paper's version is unknown.
COVERS_UPDATE = "coalesce(f.paper_updated >= p.updated, p.updated IS NULL)"

_PUBLISHED = "(" + ", ".join(f"'{s.value}'" for s in PUBLISHED) + ")"
# The topics whose published digests picked paper `p`'s current revision: a pick of text that a
# rewrite replaced was a judgement of text that no longer exists.
PICKED_BY = f"""ARRAY(
    SELECT DISTINCT d.topic FROM digest_papers dp JOIN digests d ON d.id = dp.digest_id
    WHERE dp.paper_id = p.id AND dp.role = 'pick' AND dp.paper_revision = p.revision
      AND d.state IN {_PUBLISHED})"""


@dataclass(frozen=True)
class Facts:
    """What deciding a paper's text pass reads, under its settle lock."""

    revision: int
    version: int | None
    dismissed: bool
    last: TextPass | None  # the newest text pass queued for the current revision
    has_text_result: bool  # a text pass of any revision is done


async def facts(conn: psycopg.AsyncConnection, paper_id: str) -> Facts | None:
    row = await (
        await conn.execute(
            f"""
            SELECT p.revision, p.version, p.dismissed_at IS NOT NULL AS dismissed,
                   f.id, f.kind, f.reason, f.state, f.arxiv_version,
                   NOT {COVERS_UPDATE} AS newer_update,
                   EXISTS (SELECT 1 FROM fulltext_passes d WHERE d.paper_id = p.id
                           AND d.kind <> 'deep' AND d.state = 'done') AS has_text_result
            FROM papers p
            LEFT JOIN LATERAL (
                SELECT id, kind, reason, state, arxiv_version, paper_updated FROM fulltext_passes
                WHERE paper_id = p.id AND paper_revision = p.revision AND kind <> 'deep'
                ORDER BY id DESC LIMIT 1
            ) f ON true
            WHERE p.id = %s
            """,
            (paper_id,),
        )
    ).fetchone()
    if row is None:
        return None
    last = (
        TextPass(
            row["id"],
            PassKind(row["kind"]),
            row["reason"],
            PassState(row["state"]),
            row["arxiv_version"],
            row["state"] == PassState.DONE and row["newer_update"],
        )
        if row["id"]
        else None
    )
    return Facts(row["revision"], row["version"], row["dismissed"], last, row["has_text_result"])


@dataclass(frozen=True)
class DeepFacts:
    """What deciding a paper's Deep pass reads, under its settle lock."""

    revision: int
    dismissed: bool
    open_pass: DeepPass | None  # the one not done
    has_card: bool  # of any revision
    picked_by: frozenset[str]  # the topics whose published digests picked its current revision


async def deep_facts(conn: psycopg.AsyncConnection, paper_id: str) -> DeepFacts | None:
    row = await (
        await conn.execute(
            "SELECT p.revision, p.dismissed_at IS NOT NULL AS dismissed, f.id, f.reason, f.state, "
            "EXISTS (SELECT 1 FROM deep_cards c WHERE c.paper_id = p.id AND c.card IS NOT NULL) "
            f"AS has_card, {PICKED_BY} AS picked_by FROM papers p LEFT JOIN fulltext_passes f "
            "ON f.paper_id = p.id AND f.kind = 'deep' AND f.state <> 'done' WHERE p.id = %s",
            (paper_id,),
        )
    ).fetchone()
    if row is None:
        return None
    found = DeepPass(row["id"], row["reason"], PassState(row["state"])) if row["id"] else None
    picked = frozenset(row["picked_by"])
    return DeepFacts(row["revision"], row["dismissed"], found, row["has_card"], picked)


async def request_card(conn: psycopg.AsyncConnection, paper_id: str, revision: int) -> int:
    """The owner asks for a Deep card: their request takes over the paper's open Deep pass, an
    automatic one included, so an Unsave never cancels it and it goes first in line, and a
    failed one is tried again afresh; without an open one, one is queued. Call it under the
    paper's settle lock. Returns the pass's id."""
    found = await deep_facts(conn, paper_id)
    open_pass = found.open_pass if found else None
    if open_pass is not None:
        await conn.execute(
            "UPDATE fulltext_passes SET reason = %s, priority = %s, "
            "retry_text = retry_text OR state = 'failed', "
            "failed_runs = CASE WHEN state = 'failed' THEN 0 ELSE failed_runs END, "
            "state = CASE WHEN state = 'failed' THEN 'ready' ELSE state END WHERE id = %s",
            (REQUESTED, REQUEST_PRIORITY, open_pass.id),
        )
        return open_pass.id
    return await queue(conn, paper_id, revision, PassKind.DEEP, REQUESTED, REQUEST_PRIORITY)


async def queue_deep(
    conn: psycopg.AsyncConnection, paper_id: str, revision: int, reason: str
) -> int:
    return await queue(conn, paper_id, revision, PassKind.DEEP, reason, AUTOMATIC_PRIORITY)


async def open_deep(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The paper's open Deep pass, as the paper page shows it."""
    return await (
        await conn.execute(
            "SELECT id, state, reason, queued_at, failed_runs, last_error AS error "
            "FROM fulltext_passes WHERE paper_id = %s AND kind = 'deep' AND state <> 'done'",
            (paper_id,),
        )
    ).fetchone()


async def queue(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    revision: int,
    kind: PassKind,
    reason: str,
    priority: int,
) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO fulltext_passes (paper_id, paper_revision, kind, reason, priority) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (paper_id, revision, kind.value, reason, priority),
        )
    ).fetchone()
    return row["id"]


def _unstarted(claimed: bool) -> list[str]:
    # A pass the caller has claimed but not yet worked counts as not started.
    return [s.value for s in UNSTARTED] + ([PassState.RUNNING.value] if claimed else [])


async def relabel(
    conn: psycopg.AsyncConnection, pass_id: int, kind: PassKind, reason: str, *, claimed=False
) -> None:
    """Say why a pass that has not started (or that the caller `claimed`) runs now."""
    await conn.execute(
        "UPDATE fulltext_passes SET kind = %s, reason = %s WHERE id = %s AND state = ANY(%s)",
        (kind.value, reason, pass_id, _unstarted(claimed)),
    )


async def drop(conn: psycopg.AsyncConnection, pass_id: int, *, claimed: bool = False) -> None:
    """Remove a pass that has not started (or that the caller `claimed`); it wrote nothing."""
    await conn.execute(
        "DELETE FROM fulltext_passes WHERE id = %s AND state = ANY(%s)",
        (pass_id, _unstarted(claimed)),
    )


async def drop_stale(
    conn: psycopg.AsyncConnection, paper_id: str, revision: int, claimed: int | None = None
) -> None:
    """Remove the text passes for older revisions of the paper that wrote nothing and are not
    running (not started, `claimed` but not yet started included, or their worker died): their
    text no longer exists. A running one finishes and stores its result labelled with its
    revision."""
    await conn.execute(
        "DELETE FROM fulltext_passes WHERE paper_id = %s AND paper_revision < %s "
        "AND kind <> 'deep' AND (state = ANY(%s) OR id = %s "
        "OR (state = 'running' AND lease_until < now()))",
        (paper_id, revision, [s.value for s in UNSTARTED], claimed),
    )


@dataclass(frozen=True)
class Claimed:
    """A pass a worker holds until `lease_until`: its paper as stored now, and the area its
    Weight is judged against (the primary class of its newest classification; None before
    any)."""

    id: int
    kind: PassKind
    reason: str
    revision: int  # the revision the pass was queued for
    paper: Paper
    area: TaxonomyClass | None
    lease_until: datetime
    retry_text: bool = False  # the archive is to retry the downloads it gave up on
    use_pdf: bool = False  # the owner's "Use the PDF": read its PDF, not its LaTeX


# Held by the worker that works the Deep passes, so the service's and a terminal's never run
# side by side: cards are made, and the daily cap counted, one at a time.
DEEP_WORKER_LOCK = 7213915


@asynccontextmanager
async def deep_worker(conn: psycopg.AsyncConnection) -> AsyncIterator[bool]:
    """Hold the Deep worker's lock for the block if no other worker holds it; yields whether
    it is held."""
    row = await (
        await conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (DEEP_WORKER_LOCK,))
    ).fetchone()
    try:
        yield row["locked"]
    finally:
        if row["locked"]:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (DEEP_WORKER_LOCK,))


# Whether Deep pass `f` has an attempt open: its job was submitted, or may have been.
STARTED = "EXISTS (SELECT 1 FROM deep_attempts a WHERE a.pass_id = f.id AND a.ended_at IS NULL)"


async def claim(
    conn: psycopg.AsyncConnection,
    lease: timedelta,
    limit: int | None = None,
    besides: set[int] | None = None,
    *,
    deep: bool = False,
    automatic: bool = True,
    owners: bool | None = None,
) -> list[Claimed]:
    """Take up to `limit` text passes to try, least recently tried first, `besides` those
    already tried: passes awaiting text, those to be tried again, and running ones whose lease
    ran out (the worker died). Each is held for `lease`. Only passes for their paper's current
    revision are taken (`drop_stale` removes the others); a dismissed paper's are left alone.
    With `deep`, Deep passes instead, the owner's requests first, of any revision (a Deep pass
    reads the text the paper has when it runs), and the automatic ones only when `automatic`;
    one whose job was submitted is taken whatever the cap or a dismissal says, as following its
    job spends nothing new and ends its attempt. Text passes the owner ran now only when
    `owners` is True, none of them when it is False."""
    which = (
        f"f.kind = 'deep' AND ({STARTED} OR (p.dismissed_at IS NULL "
        "AND (%(automatic)s OR f.reason = 'request')))"
        if deep
        else "f.kind <> 'deep' AND f.paper_revision = p.revision AND p.dismissed_at IS NULL"
    )
    if not deep and owners is not None:
        which += f" AND f.priority {'<' if owners else '>='} {TEXT_PRIORITY}"
    rows = await (
        await conn.execute(
            f"""
            WITH taken AS (
                SELECT f.id FROM fulltext_passes f JOIN papers p ON p.id = f.paper_id
                WHERE {which} AND NOT f.id = ANY(%(besides)s)
                  AND (f.state IN ('awaiting_text', 'ready')
                       OR (f.state = 'running' AND f.lease_until < now()))
                ORDER BY f.priority, f.tried_at NULLS FIRST, f.id
                LIMIT %(limit)s FOR UPDATE OF f SKIP LOCKED
            ), claimed AS (
                UPDATE fulltext_passes f SET state = 'running', tried_at = now(),
                                             lease_until = now() + %(lease)s
                FROM taken WHERE f.id = taken.id
                RETURNING f.id, f.kind, f.reason, f.paper_revision, f.lease_until, f.paper_id,
                          f.retry_text, f.use_pdf
            )
            SELECT {COLUMNS}, x.id AS pass_id, x.kind, x.reason, x.paper_revision AS pass_revision,
                   x.lease_until, x.retry_text, x.use_pdf,
                   k.slug, k.name, k.description, k.parent, k.threshold
            FROM claimed x JOIN papers p ON p.id = x.paper_id
            LEFT JOIN LATERAL (SELECT primary_class FROM classifications
                               WHERE paper_id = p.id
                               ORDER BY paper_revision DESC, id DESC LIMIT 1) c ON true
            LEFT JOIN classes k ON k.slug = c.primary_class
            ORDER BY x.id
            """,
            {
                "besides": list(besides or ()),
                "limit": limit,
                "lease": lease,
                "automatic": automatic,
            },
        )
    ).fetchall()
    return [
        Claimed(
            id=r["pass_id"],
            kind=PassKind(r["kind"]),
            reason=r["reason"],
            revision=r["pass_revision"],
            paper=row_to_paper(r),
            area=row_to_class(r) if r["slug"] else None,
            lease_until=r["lease_until"],
            retry_text=r["retry_text"],
            use_pdf=r["use_pdf"],
        )
        for r in rows
    ]


async def take_back(conn: psycopg.AsyncConnection, lease: timedelta) -> int:
    """End the leases on text passes taken for longer than `lease`: a batching nightly's, which a
    nightly holding the run lock calls, so the run that held them is dead. Returns how many. The
    taxonomy lock is taken shared first, as in `retry_failed`."""
    async with conn.transaction():
        await lock_shared(conn)
        cur = await conn.execute(
            "UPDATE fulltext_passes SET lease_until = now() - interval '1 second' "
            "WHERE kind <> 'deep' AND state = 'running' AND lease_until - tried_at > %s",
            (lease,),
        )
    return cur.rowcount


async def held(conn: psycopg.AsyncConnection, c: Claimed) -> bool:
    """Whether the worker still holds the pass, its row locked to the end of the transaction:
    no other worker took it over after its lease ran out."""
    row = await (
        await conn.execute(
            "SELECT 1 FROM fulltext_passes WHERE id = %s AND state = 'running' "
            "AND lease_until = %s FOR UPDATE",
            (c.id, c.lease_until),
        )
    ).fetchone()
    return row is not None


async def label_of(conn: psycopg.AsyncConnection, pass_id: int) -> tuple[PassKind, str] | None:
    """The pass's kind and reason, or None when it is gone (dropped)."""
    row = await (
        await conn.execute("SELECT kind, reason FROM fulltext_passes WHERE id = %s", (pass_id,))
    ).fetchone()
    return (PassKind(row["kind"]), row["reason"]) if row else None


async def release(conn: psycopg.AsyncConnection, c: Claimed) -> None:
    """Give a claimed pass back unworked: it awaits its next claim, behind those not tried."""
    await conn.execute(
        "UPDATE fulltext_passes SET state = 'awaiting_text', lease_until = NULL "
        "WHERE id = %s AND state = 'running' AND lease_until = %s",
        (c.id, c.lease_until),
    )


async def let_go(conn: psycopg.AsyncConnection, c: Claimed, error: str) -> None:
    """End the worker's lease on a pass it started, now: it stays running, so no settle drops
    it, and the next claim takes it over at once."""
    await conn.execute(
        "UPDATE fulltext_passes SET lease_until = now(), last_error = %s "
        "WHERE id = %s AND state = 'running' AND lease_until = %s",
        (error[:2000], c.id, c.lease_until),
    )


async def retry_asked(conn: psycopg.AsyncConnection, c: Claimed) -> None:
    """The archive was asked to retry what it gave up on for the pass."""
    await conn.execute("UPDATE fulltext_passes SET retry_text = false WHERE id = %s", (c.id,))


async def dismissed(conn: psycopg.AsyncConnection, paper_id: str) -> bool:
    """Whether the paper is dismissed, read with a share lock that waits for a dismissal in
    progress (as `papers.still_current`)."""
    row = await (
        await conn.execute(
            "SELECT dismissed_at IS NOT NULL AS dismissed FROM papers WHERE id = %s FOR SHARE",
            (paper_id,),
        )
    ).fetchone()
    return row is not None and row["dismissed"]


async def awaiting(conn: psycopg.AsyncConnection, c: Claimed, error: str | None = None) -> None:
    """The archive has no text for it yet (or cannot be reached: `error`): it waits again."""
    await conn.execute(
        "UPDATE fulltext_passes SET state = 'awaiting_text', lease_until = NULL, "
        "last_error = %s WHERE id = %s AND state = 'running' AND lease_until = %s",
        (error, c.id, c.lease_until),
    )


def _read(text: TextRead | None) -> dict[str, Any]:
    return {
        "version": text.version if text else None,
        "source": text.source if text else None,
        "hash": text.sha256 if text else None,
        "arrived": text is not None,
    }


async def failed(
    conn: psycopg.AsyncConnection,
    c: Claimed,
    error: str,
    *,
    counts: bool,
    text: TextRead | None = None,
) -> None:
    """The pass failed this run; it is tried again on the next. Only a failure that `counts`
    (no text to be had, an unusable answer) adds a failed run. Call it in a transaction that
    `held` the pass."""
    await conn.execute(
        """
        UPDATE fulltext_passes SET state = 'failed', lease_until = NULL, last_error = %(error)s,
            failed_runs = failed_runs + %(counts)s,
            text_arrived_at = CASE WHEN %(arrived)s THEN coalesce(text_arrived_at, now())
                                   ELSE text_arrived_at END,
            arxiv_version = CASE WHEN %(arrived)s THEN %(version)s ELSE arxiv_version END,
            text_source = coalesce(%(source)s, text_source),
            text_hash = coalesce(%(hash)s, text_hash)
        WHERE id = %(id)s
        """,
        {"id": c.id, "error": error[:2000], "counts": int(counts), **_read(text)},
    )


@dataclass(frozen=True)
class Written:
    """The ids of what one pass wrote."""

    brief: int
    gist: int


async def add(
    conn: psycopg.AsyncConnection,
    c: Claimed,
    text: TextRead,
    *,
    brief: str,
    overstatement: str | None,
    gist: str,
    weight: Weight,
    weight_reason: str,
    model: str,
) -> Written:
    """Store what the pass wrote, labelled with the revision it was queued for and the version
    it read, and mark it done. Call it in a transaction that `held` the pass."""
    pass_row = (c.paper.id, c.revision, text.version)
    brief_row = await (
        await conn.execute(
            "INSERT INTO briefs (paper_id, paper_revision, arxiv_version, pass_id, text, "
            "overstatement, text_source, text_hash, model) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (*pass_row, c.id, brief, overstatement, text.source, text.sha256, model),
        )
    ).fetchone()
    await conn.execute(
        "INSERT INTO weights (paper_id, paper_revision, weight, source, reason, model, pass_id) "
        "VALUES (%s, %s, %s, 'full_text', %s, %s, %s)",
        (c.paper.id, c.revision, weight.value, weight_reason, model, c.id),
    )
    gist_row = await (
        await conn.execute(
            "INSERT INTO gists (paper_id, paper_revision, arxiv_version, text, source, model, "
            "pass_id) VALUES (%s, %s, %s, %s, 'full_text', %s, %s) RETURNING id",
            (*pass_row, gist, model, c.id),
        )
    ).fetchone()
    await done(conn, c, text)
    return Written(brief_row["id"], gist_row["id"])


async def done(conn: psycopg.AsyncConnection, c: Claimed, text: TextRead) -> None:
    """The pass wrote what it writes from `text`. Call it in a transaction that `held` it."""
    await conn.execute(
        """
        UPDATE fulltext_passes SET state = 'done', lease_until = NULL, last_error = NULL,
            finished_at = now(), text_arrived_at = coalesce(text_arrived_at, now()),
            arxiv_version = %(version)s, text_source = %(source)s, text_hash = %(hash)s,
            paper_updated = %(updated)s::timestamptz
        WHERE id = %(id)s
        """,
        {"id": c.id, "updated": c.paper.updated, **_read(text)},
    )


async def submitted(conn: psycopg.AsyncConnection, c: Claimed, text: TextRead) -> str | None:
    """A Deep pass's text is about to be sent: the pass records what it read, of the paper's
    revision now, which is what its card is labelled with, even when the job is followed by a
    later claim. Returns the pass's reason now (the owner's request may have taken it over since
    the claim), or None when the worker no longer holds it."""
    row = await (
        await conn.execute(
            """
        UPDATE fulltext_passes SET paper_revision = %(revision)s,
            text_arrived_at = coalesce(text_arrived_at, now()), arxiv_version = %(version)s,
            text_source = %(source)s, text_hash = %(hash)s, paper_updated = %(updated)s::timestamptz
        WHERE id = %(id)s AND state = 'running' AND lease_until = %(lease)s
        RETURNING reason
        """,
            {
                "id": c.id,
                "lease": c.lease_until,
                "revision": c.paper.revision,
                "updated": c.paper.updated,
                **_read(text),
            },
        )
    ).fetchone()
    return row["reason"] if row else None


async def read_of(conn: psycopg.AsyncConnection, pass_id: int) -> tuple[int, TextRead | None]:
    """The paper revision a pass read and what it read (None before it read any text)."""
    row = await (
        await conn.execute(
            "SELECT paper_revision, arxiv_version, text_source, text_hash FROM fulltext_passes "
            "WHERE id = %s",
            (pass_id,),
        )
    ).fetchone()
    read = (
        TextRead(row["arxiv_version"], row["text_source"], row["text_hash"])
        if row["text_hash"]
        else None
    )
    return row["paper_revision"], read


async def outdated(conn: psycopg.AsyncConnection) -> list[str]:
    """The papers, not dismissed, given a Brief or full-text Weight, or with a text pass for an
    older revision, whose newest text pass for its current revision is neither open nor done on
    its arXiv version (on its latest update, when the version is unknown): a new version or
    revision came after what was read or queued (`to_queue`'s rule, found from the
    database)."""
    rows = await (await conn.execute(f"{OUTDATED} ORDER BY p.id")).fetchall()
    return [r["id"] for r in rows]


OUTDATED = f"""
    SELECT DISTINCT p.id FROM fulltext_passes d JOIN papers p ON p.id = d.paper_id
    WHERE d.kind <> 'deep' AND (d.state = 'done' OR d.paper_revision < p.revision)
      AND p.dismissed_at IS NULL
      -- Unless the newest pass for the current revision, the one `facts` reads, is open or read
      -- the paper's version (its latest update, the version unknown).
      AND NOT coalesce((
          SELECT f.state <> 'done' OR f.arxiv_version >= p.version
                 OR (p.version IS NULL AND {COVERS_UPDATE})
          FROM fulltext_passes f
          WHERE f.paper_id = p.id AND f.paper_revision = p.revision AND f.kind <> 'deep'
          ORDER BY f.id DESC LIMIT 1), false)
"""


async def candidates(conn: psycopg.AsyncConnection, lookback: timedelta) -> list[str]:
    """The papers, not dismissed, whose passes the nightly run settles from the database
    whatever triggered or missed it (pipeline.md, section 6): every paper with a
    pass open; every one without a text pass for its current revision that the rule may give one
    (an active Focus topic's central or related, an active Area's must read, its own or a
    followed descendant's; or such a verdict or label from the last `lookback`, whose settle
    may have been missed); the `outdated` ones; every saved Focus must read with no card,
    or one with a recent Focus verdict or label of any value, whatever text pass it has; and
    every pick of a published digest of an active Area topic with no card, not unsaved.
    Bounded by the queue and what qualifies but has no pass, not by the library; never cut
    short, as settling spends nothing and a cut could leave the same papers out every night."""
    rows = await (
        await conn.execute(
            f"""
            WITH RECURSIVE area_scope AS (
                SELECT class AS root, class FROM topics
                WHERE depth = 'area' AND status = 'active'
                UNION
                SELECT a.root, c.slug FROM classes c JOIN area_scope a ON c.parent = a.class
                WHERE c.retired_at IS NULL
            ), focus AS (
                SELECT class FROM topics WHERE depth = 'focus' AND status = 'active'
            ), area AS (
                SELECT a.root, t.class FROM topics t JOIN area_scope a ON a.class = t.class
                WHERE t.status <> 'draft'
            ), area_must_read AS (
                -- An Area's own grade, else its followed descendants' highest
                -- (core.grading.page_standing); must read has the highest rarity.
                SELECT g.paper_id FROM effective_grades g JOIN area a ON a.class = g.topic
                WHERE g.grade = 'must_read' AND (g.topic = a.root OR NOT EXISTS (
                    SELECT 1 FROM effective_grades own
                    WHERE own.paper_id = g.paper_id AND own.topic = a.root))
            ), recent AS (
                -- Verdicts and labels whose settle may have been missed, of any value: an
                -- unsure label can let an older verdict count again.
                SELECT paper_id, topic, fit AS value FROM verdicts
                WHERE recorded_at >= now() - %(lookback)s
                UNION
                SELECT paper_id, topic, value FROM labels
                WHERE labelled_at >= now() - %(lookback)s
            ), area_picks AS (
                -- Picks of the current revision in active Area topics' published digests.
                SELECT dp.paper_id FROM digest_papers dp
                JOIN digests d ON d.id = dp.digest_id
                JOIN topics t ON t.class = d.topic AND t.depth = 'area' AND t.status = 'active'
                JOIN papers pp ON pp.id = dp.paper_id AND pp.revision = dp.paper_revision
                WHERE dp.role = 'pick' AND d.state IN {_PUBLISHED}
            ), qualifying AS (
                SELECT paper_id FROM effective_grades
                WHERE topic IN (SELECT class FROM focus) AND fit IN ('central', 'related')
                UNION
                SELECT paper_id FROM area_must_read
                UNION
                SELECT paper_id FROM recent WHERE value IN ('central', 'related')
                AND (topic IN (SELECT class FROM focus) OR topic IN (SELECT class FROM area))
            )
            SELECT p.id FROM papers p
            WHERE p.dismissed_at IS NULL AND (
                EXISTS (SELECT 1 FROM fulltext_passes f WHERE f.paper_id = p.id
                        AND f.state <> 'done')
                OR (p.id IN (SELECT paper_id FROM qualifying)
                    AND NOT EXISTS (SELECT 1 FROM fulltext_passes f WHERE f.paper_id = p.id
                                    AND f.paper_revision = p.revision AND f.kind <> 'deep'))
                OR p.id IN ({OUTDATED})
                -- A Focus must read, saved, or a recent Focus verdict or label, with no card:
                -- it may want a Deep pass, a Brief pass of its own or not.
                OR ((EXISTS (SELECT 1 FROM effective_grades g WHERE g.paper_id = p.id
                             AND g.topic IN (SELECT class FROM focus) AND g.grade = 'must_read')
                     AND EXISTS (SELECT 1 FROM library l WHERE l.paper_id = p.id AND l.saved)
                     OR p.id IN (SELECT paper_id FROM recent
                                 WHERE topic IN (SELECT class FROM focus)))
                    AND NOT EXISTS (SELECT 1 FROM deep_cards c WHERE c.paper_id = p.id
                                    AND c.card IS NOT NULL))
                -- An Area pick with no card, not unsaved: it may want a Deep pass.
                OR (p.id IN (SELECT paper_id FROM area_picks)
                    AND NOT EXISTS (SELECT 1 FROM library l WHERE l.paper_id = p.id
                                    AND l.hand = 'unsave')
                    AND NOT EXISTS (SELECT 1 FROM deep_cards c WHERE c.paper_id = p.id
                                    AND c.card IS NOT NULL))
            )
            ORDER BY p.id
            """,
            {"lookback": lookback},
        )
    ).fetchall()
    return [r["id"] for r in rows]


async def retry_failed(conn: psycopg.AsyncConnection, runs: int) -> int:
    """Make every failed pass with fewer than `runs` failed runs ready: the run tries it again,
    and the archive what it gave up on. The taxonomy lock is taken shared first, as every
    settle takes it, so this never deadlocks with a taxonomy save dropping passes."""
    async with conn.transaction():
        await lock_shared(conn)
        cur = await conn.execute(
            "UPDATE fulltext_passes SET state = 'ready', retry_text = true "
            "WHERE state = 'failed' AND failed_runs < %s",
            (runs,),
        )
    return cur.rowcount


# A pass that waits on the owner: failed `runs` runs in a row, or awaiting text too long. Of
# `fulltext_passes f`; the paper (`p`) is not dismissed, and a text pass is for its current
# revision. A Deep pass awaits text only once its worker has tried it: with no deep service
# configured, nothing works it, and nothing waits on the owner.
STUCK = (
    "p.dismissed_at IS NULL AND (f.kind = 'deep' OR f.paper_revision = p.revision) "
    "AND ((f.state = 'failed' AND f.failed_runs >= %(runs)s) "
    "OR (f.state = 'awaiting_text' AND f.queued_at < now() - %(long)s "
    "AND (f.kind <> 'deep' OR f.tried_at IS NOT NULL)))"
)


async def stuck(conn: psycopg.AsyncConnection, runs: int) -> list[dict[str, Any]]:
    """The passes waiting on the owner, with their paper's title, most failed first."""
    return await (
        await conn.execute(
            "SELECT f.id, f.paper_id, f.paper_revision AS revision, p.title, f.kind, f.state, "
            "f.failed_runs, f.last_error, f.queued_at FROM fulltext_passes f "
            f"JOIN papers p ON p.id = f.paper_id WHERE {STUCK} "
            "ORDER BY f.failed_runs DESC, f.queued_at, f.id",
            stuck_params(runs),
        )
    ).fetchall()


async def count_stuck(conn: psycopg.AsyncConnection, runs: int) -> int:
    row = await (
        await conn.execute(
            "SELECT count(*) AS n FROM fulltext_passes f JOIN papers p ON p.id = f.paper_id "
            f"WHERE {STUCK}",
            stuck_params(runs),
        )
    ).fetchone()
    return row["n"]


async def paper_of(conn: psycopg.AsyncConnection, pass_id: int) -> str | None:
    row = await (
        await conn.execute("SELECT paper_id FROM fulltext_passes WHERE id = %s", (pass_id,))
    ).fetchone()
    return row["paper_id"] if row else None


async def run_now(
    conn: psycopg.AsyncConnection, pass_id: int, runs: int, *, pdf: bool = False
) -> bool:
    """The owner's "run it now" on a pass waiting for them: three more runs, and first in line.
    A Deep pass becomes the owner's request, ahead of the automatic ones and past the daily cap;
    a text pass too, ahead of them and the service worker's while a nightly runs.
    With `pdf` ("Use the PDF"), it reads the paper's PDF from then on, not its LaTeX. Returns
    False when it does not wait for the owner."""
    cur = await conn.execute(
        "UPDATE fulltext_passes f SET state = 'ready', failed_runs = 0, tried_at = NULL, "
        "queued_at = now(), retry_text = true, use_pdf = f.use_pdf OR %(pdf)s, "
        "reason = CASE WHEN f.kind = 'deep' THEN %(requested)s ELSE f.reason END, "
        "priority = CASE WHEN f.kind = 'deep' THEN %(first)s ELSE %(text_first)s END "
        f"FROM papers p WHERE f.id = %(id)s AND p.id = f.paper_id AND {STUCK}",
        {
            "id": pass_id,
            "requested": REQUESTED,
            "first": REQUEST_PRIORITY,
            "text_first": TEXT_REQUEST_PRIORITY,
            "pdf": pdf,
        }
        | stuck_params(runs),
    )
    return cur.rowcount == 1


# Whether paper `p` has a text pass waiting on the owner, for a query with the parameters
# `runs` and `long` (`stuck_params`).
STUCK_EXISTS = f"EXISTS (SELECT 1 FROM fulltext_passes f WHERE f.paper_id = p.id AND {STUCK})"


def stuck_params(runs: int) -> dict[str, Any]:
    return {"runs": runs, "long": AWAITING_TOO_LONG}


async def brief_for(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The paper's Brief: from the pass queued last for its current revision, else the newest
    from an older revision, kept until a new one exists (pipeline.md, section 8)."""
    return await (
        await conn.execute(
            "SELECT b.id, b.text, b.overstatement, b.paper_revision AS revision, b.arxiv_version, "
            "b.text_source AS source, b.at AS written_at FROM briefs b "
            "JOIN papers p ON p.id = b.paper_id WHERE b.paper_id = %s "
            "ORDER BY b.paper_revision = p.revision DESC, b.paper_revision DESC, b.pass_id DESC "
            "LIMIT 1",
            (paper_id,),
        )
    ).fetchone()


async def text_pass_for(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """The text pass queued last for the paper's current revision: why it has a Brief or not."""
    return await (
        await conn.execute(
            "SELECT f.id, f.kind, f.state, f.queued_at, f.arxiv_version FROM fulltext_passes f "
            "JOIN papers p ON p.id = f.paper_id AND p.revision = f.paper_revision "
            "WHERE f.paper_id = %s AND f.kind <> 'deep' ORDER BY f.id DESC LIMIT 1",
            (paper_id,),
        )
    ).fetchone()


async def count_by_state(conn: psycopg.AsyncConnection) -> dict[tuple[str, str], int]:
    """Passes not done, by (kind, state), every pair present: a drained queue reads 0."""
    rows = await (
        await conn.execute(
            "SELECT kind, state, count(*) AS n FROM fulltext_passes WHERE state <> 'done' "
            "GROUP BY kind, state"
        )
    ).fetchall()
    counts = {(k.value, s.value): 0 for k in PassKind for s in PassState if s != PassState.DONE}
    return counts | {(r["kind"], r["state"]): r["n"] for r in rows}
