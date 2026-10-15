"""The full-text queue and what its passes wrote (data.md: `fulltext_passes`, `briefs`, and the
full-text rows of `weights` and `gists`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from litwatch.core import Paper, TaxonomyClass, Weight
from litwatch.core.fulltext import (
    AWAITING_TOO_LONG,
    UNSTARTED,
    PassKind,
    PassState,
    TextPass,
    TextRead,
)
from litwatch.store.papers import COLUMNS, row_to_paper
from litwatch.store.taxonomy import row_to_class


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
            """
            SELECT p.revision, p.version, p.dismissed_at IS NOT NULL AS dismissed,
                   f.id, f.kind, f.reason, f.state, f.arxiv_version,
                   EXISTS (SELECT 1 FROM fulltext_passes d WHERE d.paper_id = p.id
                           AND d.kind <> 'deep' AND d.state = 'done') AS has_text_result
            FROM papers p
            LEFT JOIN LATERAL (
                SELECT id, kind, reason, state, arxiv_version FROM fulltext_passes
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
        )
        if row["id"]
        else None
    )
    return Facts(row["revision"], row["version"], row["dismissed"], last, row["has_text_result"])


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


async def claim(
    conn: psycopg.AsyncConnection,
    lease: timedelta,
    limit: int | None = None,
    besides: set[int] | None = None,
) -> list[Claimed]:
    """Take up to `limit` text passes to try, least recently tried first, `besides` those
    already tried: passes awaiting text, those to be tried again, and running ones whose lease
    ran out (the worker died). Each is held for `lease`. Only passes for their paper's current
    revision are taken (`drop_stale` removes the others); a dismissed paper's are left alone."""
    rows = await (
        await conn.execute(
            f"""
            WITH taken AS (
                SELECT f.id FROM fulltext_passes f JOIN papers p ON p.id = f.paper_id
                WHERE f.kind <> 'deep' AND p.dismissed_at IS NULL
                  AND f.paper_revision = p.revision AND NOT f.id = ANY(%s)
                  AND (f.state IN ('awaiting_text', 'ready')
                       OR (f.state = 'running' AND f.lease_until < now()))
                ORDER BY f.priority, f.tried_at NULLS FIRST, f.id
                LIMIT %s FOR UPDATE OF f SKIP LOCKED
            ), claimed AS (
                UPDATE fulltext_passes f SET state = 'running', tried_at = now(),
                                             lease_until = now() + %s
                FROM taken WHERE f.id = taken.id
                RETURNING f.id, f.kind, f.reason, f.paper_revision, f.lease_until, f.paper_id,
                          f.retry_text
            )
            SELECT {COLUMNS}, x.id AS pass_id, x.kind, x.reason, x.paper_revision AS pass_revision,
                   x.lease_until, x.retry_text, k.slug, k.name, k.description, k.parent, k.threshold
            FROM claimed x JOIN papers p ON p.id = x.paper_id
            LEFT JOIN LATERAL (SELECT primary_class FROM classifications
                               WHERE paper_id = p.id
                               ORDER BY paper_revision DESC, id DESC LIMIT 1) c ON true
            LEFT JOIN classes k ON k.slug = c.primary_class
            ORDER BY x.id
            """,
            (list(besides or ()), limit, lease),
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
        )
        for r in rows
    ]


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
    await conn.execute(
        """
        UPDATE fulltext_passes SET state = 'done', lease_until = NULL, last_error = NULL,
            finished_at = now(), text_arrived_at = coalesce(text_arrived_at, now()),
            arxiv_version = %(version)s, text_source = %(source)s, text_hash = %(hash)s
        WHERE id = %(id)s
        """,
        {"id": c.id, **_read(text)},
    )
    return Written(brief_row["id"], gist_row["id"])


async def outdated(conn: psycopg.AsyncConnection) -> list[str]:
    """The papers, not dismissed, given a Brief or full-text Weight, or with a text pass for an
    older revision, whose current revision has no text pass open or done on its arXiv version: a
    new version or revision came after what was read or queued (`to_queue`'s rule, found from
    the database)."""
    rows = await (await conn.execute(f"{OUTDATED} ORDER BY p.id")).fetchall()
    return [r["id"] for r in rows]


OUTDATED = """
    SELECT DISTINCT p.id FROM fulltext_passes d JOIN papers p ON p.id = d.paper_id
    WHERE d.kind <> 'deep' AND (d.state = 'done' OR d.paper_revision < p.revision)
      AND p.dismissed_at IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM fulltext_passes f
          WHERE f.paper_id = p.id AND f.paper_revision = p.revision AND f.kind <> 'deep'
            AND (f.state <> 'done' OR p.version IS NULL OR f.arxiv_version >= p.version))
"""


async def candidates(conn: psycopg.AsyncConnection, lookback: timedelta) -> list[str]:
    """The papers, not dismissed, whose text pass the nightly run settles from the database
    whatever triggered or missed it (pipeline.md, section 6): every paper with a
    pass open; every one without a pass for its current revision that the rule may give one
    (an active Focus topic's central or related, an active Area's must read, its own or a
    followed descendant's; or such a verdict or label from the last `lookback`, whose settle
    may have been missed); and the `outdated` ones. Bounded by the queue and what qualifies but
    has no pass, not by the library; never cut short, as settling spends nothing and a cut
    could leave the same papers out every night."""
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
            ), qualifying AS (
                SELECT paper_id FROM effective_grades
                WHERE topic IN (SELECT class FROM focus) AND fit IN ('central', 'related')
                UNION
                SELECT paper_id FROM area_must_read
                UNION
                SELECT paper_id FROM verdicts
                WHERE recorded_at >= now() - %(lookback)s AND fit IN ('central', 'related')
                  AND (topic IN (SELECT class FROM focus) OR topic IN (SELECT class FROM area))
                UNION
                SELECT paper_id FROM labels
                WHERE labelled_at >= now() - %(lookback)s AND value IN ('central', 'related')
                  AND (topic IN (SELECT class FROM focus) OR topic IN (SELECT class FROM area))
            )
            SELECT p.id FROM papers p
            WHERE p.dismissed_at IS NULL AND (
                EXISTS (SELECT 1 FROM fulltext_passes f WHERE f.paper_id = p.id
                        AND f.kind <> 'deep' AND f.state <> 'done')
                OR (p.id IN (SELECT paper_id FROM qualifying)
                    AND NOT EXISTS (SELECT 1 FROM fulltext_passes f WHERE f.paper_id = p.id
                                    AND f.paper_revision = p.revision AND f.kind <> 'deep'))
                OR p.id IN ({OUTDATED})
            )
            ORDER BY p.id
            """,
            {"lookback": lookback},
        )
    ).fetchall()
    return [r["id"] for r in rows]


async def retry_failed(conn: psycopg.AsyncConnection, runs: int) -> int:
    """Make every failed pass with fewer than `runs` failed runs ready: the run tries it again,
    and the archive what it gave up on."""
    cur = await conn.execute(
        "UPDATE fulltext_passes SET state = 'ready', retry_text = true "
        "WHERE state = 'failed' AND failed_runs < %s",
        (runs,),
    )
    return cur.rowcount


# A text pass that waits on the owner: failed `runs` runs in a row, or awaiting text too long. Of
# `fulltext_passes f`; the paper is current (`p`) and not dismissed.
STUCK = (
    "f.kind <> 'deep' AND p.dismissed_at IS NULL AND f.paper_revision = p.revision "
    "AND ((f.state = 'failed' "
    "AND f.failed_runs >= %(runs)s) OR (f.state = 'awaiting_text' "
    "AND f.queued_at < now() - %(long)s))"
)


async def stuck(conn: psycopg.AsyncConnection, runs: int) -> list[dict[str, Any]]:
    """The text passes waiting on the owner, with their paper's title, most failed first."""
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


async def run_now(conn: psycopg.AsyncConnection, pass_id: int, runs: int) -> bool:
    """The owner's "run it now" on a pass waiting for them: three more runs, and first in line.
    Returns False when it does not wait for the owner."""
    cur = await conn.execute(
        "UPDATE fulltext_passes f SET state = 'ready', failed_runs = 0, tried_at = NULL, "
        "queued_at = now(), retry_text = true FROM papers p "
        f"WHERE f.id = %(id)s AND p.id = f.paper_id AND {STUCK}",
        {"id": pass_id} | stuck_params(runs),
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
