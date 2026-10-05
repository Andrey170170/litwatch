"""PostgreSQL store: papers, verdicts, selections, runs. One async connection per unit of work."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from litwatch.archive_outbox import ArchivePolicy, enqueue
from litwatch.models import NOT_MEMBER, Paper, ScreenResult, Verdict
from litwatch.selection import Selection, Status, versions_hash


async def connect(dsn: str) -> psycopg.AsyncConnection[dict[str, Any]]:
    return await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row, autocommit=True)


async def apply_schema(conn: psycopg.AsyncConnection) -> None:
    from litwatch.digest_history import seed_history

    await conn.execute(resources.files("litwatch").joinpath("schema.sql").read_text())
    await seed_history(conn)


# -- papers ---------------------------------------------------------------------------------------


async def upsert_papers(
    conn: psycopg.AsyncConnection,
    papers: list[Paper],
    *,
    source: str = "arxiv",
    archive_policy: ArchivePolicy | None = None,
    archive_run_id: int | None = None,
) -> tuple[int, int]:
    """Insert new papers, refresh metadata on replacements. Returns (inserted, updated).

    Screening state is kept on update unless canonical metadata rewrote the screened text
    (`rewritten`): then the paper is screened again, since its verdicts judged other text.
    Atom metadata can insert candidates but never replace canonical metadata.
    """
    inserted = updated = 0
    async with conn.transaction():
        screened = {
            r["id"]: r
            for r in await (
                await conn.execute(
                    "SELECT id, title, abstract FROM papers WHERE id = ANY(%s) "
                    "AND screened_at IS NOT NULL",
                    ([p.id for p in papers],),
                )
            ).fetchall()
        }
        async with conn.cursor() as cur:
            for p in papers:
                await cur.execute(
                    """
                    INSERT INTO papers (id, title, abstract, authors, categories, primary_category,
                                        published, updated, url, metadata_source,
                                        version, version_history,
                                        doi, journal_ref, license)
                    VALUES (%(id)s, %(title)s, %(abstract)s, %(authors)s, %(categories)s,
                            %(primary_category)s, %(published)s, %(updated)s, %(url)s, %(source)s,
                            %(version)s, %(version_history)s, %(doi)s, %(journal_ref)s, %(license)s)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title, abstract = EXCLUDED.abstract,
                        authors = EXCLUDED.authors, categories = EXCLUDED.categories,
                        primary_category = coalesce(EXCLUDED.primary_category,
                                                    papers.primary_category),
                        published = least(papers.published, EXCLUDED.published),
                        updated = CASE WHEN %(source)s = 'oai'
                                       AND papers.updated::date = EXCLUDED.updated::date
                                       THEN papers.updated ELSE EXCLUDED.updated END,
                        url = EXCLUDED.url, metadata_source = EXCLUDED.metadata_source,
                        version = CASE WHEN EXCLUDED.version IS NOT NULL THEN EXCLUDED.version
                            WHEN EXCLUDED.updated IS NOT NULL AND (papers.updated IS NULL
                                 OR EXCLUDED.updated > papers.updated) THEN NULL
                            ELSE papers.version END,
                        version_history = CASE
                            WHEN EXCLUDED.version_history <> '[]'::jsonb
                                 THEN EXCLUDED.version_history
                            WHEN EXCLUDED.version IS NULL AND EXCLUDED.updated IS NOT NULL
                                 AND (papers.updated IS NULL OR EXCLUDED.updated > papers.updated)
                                 THEN '[]'::jsonb
                            ELSE papers.version_history END,
                        doi = coalesce(EXCLUDED.doi,papers.doi),
                        journal_ref = coalesce(EXCLUDED.journal_ref,papers.journal_ref),
                        license = coalesce(EXCLUDED.license,papers.license),
                        -- What the lab sees (Notion) changed: the projection picks it up.
                        metadata_changed_at = CASE
                            WHEN (papers.title, papers.abstract, papers.authors, papers.published)
                                 IS DISTINCT FROM (EXCLUDED.title, EXCLUDED.abstract,
                                    EXCLUDED.authors, least(papers.published, EXCLUDED.published))
                            THEN now() ELSE papers.metadata_changed_at END
                    WHERE %(source)s <> 'atom'
                      AND (papers.version IS NULL OR EXCLUDED.version IS NULL
                           OR EXCLUDED.version >= papers.version)
                      AND (%(source)s <> 'oai' OR papers.updated IS NULL
                           OR EXCLUDED.updated::date >= papers.updated::date)
                    RETURNING (xmax = 0) AS inserted
                    """,
                    {
                        **p.model_dump(),
                        "authors": Jsonb(p.authors),
                        "version_history": Jsonb(p.version_history),
                        "source": source,
                    },
                )
                row = await cur.fetchone()
                if row and row["inserted"]:
                    inserted += 1
                elif row:
                    updated += 1
                    if (old := screened.get(p.id)) and rewritten(old["title"], old["abstract"], p):
                        await cur.execute(
                            "UPDATE papers SET screened_at = NULL WHERE id = %s", (p.id,)
                        )
        if archive_policy is not None:
            await enqueue(conn, papers, archive_policy, source=source, run_id=archive_run_id)
    return inserted, updated


REWRITE_SIMILARITY = 0.8  # texts less similar than this were rewritten, not touched up


def rewritten(title: str, abstract: str, new: Paper) -> bool:
    """A title or abstract changed beyond typo, wording and Atom-vs-canonical formatting."""

    def differs(old: str, current: str) -> bool:
        old, current = " ".join(old.split()).casefold(), " ".join(current.split()).casefold()
        return (
            old != current
            and SequenceMatcher(None, old, current, autojunk=False).ratio() < REWRITE_SIMILARITY
        )

    return differs(title, new.title) or differs(abstract, new.abstract)


def nul_free(value: Any) -> Any:
    """`value` with NUL characters replaced, recursively: PostgreSQL text and jsonb reject them.

    Extracted paper text and agent cell output occasionally carry a stray NUL byte; one such
    character must not make a whole write fail.
    """
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {nul_free(k): nul_free(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [nul_free(v) for v in value]
    return value


def row_to_paper(row: dict[str, Any]) -> Paper:
    return Paper(
        id=row["id"],
        title=row["title"],
        abstract=row["abstract"],
        authors=list(row["authors"] or []),
        categories=list(row["categories"] or []),
        primary_category=row.get("primary_category"),
        published=_iso(row.get("published")),
        updated=_iso(row.get("updated")),
        url=row.get("url"),
        version=row.get("version"),
        version_history=row.get("version_history") or [],
        doi=row.get("doi"),
        journal_ref=row.get("journal_ref"),
        license=row.get("license"),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def pending(
    conn: psycopg.AsyncConnection,
    selections: list[Selection],
    window_days: int = 30,
    limit: int = 20000,
) -> list[Paper]:
    """Papers that need a screening call under the given selections.

    New papers always. Papers whose stored judgement for some selection is missing or predates
    its current version: for an edited selection its current members whatever their age plus
    keyword hits in the window; a selection with no verdicts at all is new and takes the whole
    window.
    """
    current = {s.slug: s.version for s in selections}
    since = datetime.now(UTC) - timedelta(days=window_days)
    new = await (
        await conn.execute(
            "SELECT * FROM papers WHERE screened_at IS NULL ORDER BY fetched_at LIMIT %s", (limit,)
        )
    ).fetchall()
    chosen = {r["id"]: r for r in new}
    fresh = {s.slug for s in selections if not await has_verdicts(conn, s.slug)}
    for s in selections:
        rows = await (
            await conn.execute(
                """
                SELECT p.*, coalesce((SELECT v.tier FROM verdicts v
                                      WHERE v.paper_id = p.id AND v.selection = %(slug)s), 4) AS t
                FROM papers p
                WHERE p.screened_at IS NOT NULL
                  AND (p.published >= %(since)s
                       OR EXISTS (SELECT 1 FROM verdicts v
                                  WHERE v.paper_id = p.id AND v.selection = %(slug)s
                                    AND v.tier < 4))
                  AND NOT EXISTS (SELECT 1 FROM verdicts v
                                  WHERE v.paper_id = p.id AND v.selection = %(slug)s
                                    AND v.version = %(version)s)
                ORDER BY p.published DESC LIMIT %(limit)s
                """,
                {"slug": s.slug, "version": current[s.slug], "since": since, "limit": limit},
            )
        ).fetchall()
        for r in rows:
            if r["id"] in chosen:
                continue
            if s.slug in fresh or r["t"] < NOT_MEMBER or s.keyword_hit(row_to_paper(r)):
                chosen[r["id"]] = r
    return [row_to_paper(r) for r in chosen.values()]


async def has_verdicts(conn: psycopg.AsyncConnection, slug: str) -> bool:
    row = await (
        await conn.execute("SELECT 1 AS x FROM verdicts WHERE selection = %s LIMIT 1", (slug,))
    ).fetchone()
    return row is not None


# Stamped when a member (best tier 1..3) stops being one, cleared when it is a member again:
# the projection archives the page it made for the paper. `{new}` is the incoming best tier.
DEMOTED_AT = """CASE WHEN {new} < 4 THEN NULL
                     WHEN best_tier < 4 THEN clock_timestamp() ELSE demoted_at END"""


async def store_screen(conn: psycopg.AsyncConnection, result: ScreenResult) -> None:
    """The staged JSON on the paper, one verdict row per selection, and the paper's best tier."""
    from litwatch.digest_history import DIGEST_LOCK

    verdicts = result.verdicts
    async with conn.transaction():
        # Let a digest finish its snapshot/coverage before accepting concurrent new analysis.
        await conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (DIGEST_LOCK,))
        await conn.execute(
            """
            UPDATE papers SET screened_at = clock_timestamp(), screen = %s,
                              demoted_at = {demoted}, best_tier = %s,
                              prompt_version = %s, selections_version = %s, screen_error = NULL
            WHERE id = %s
            """.format(demoted=DEMOTED_AT.format(new="%s")),
            (
                Jsonb(result.model_dump(mode="json")),
                result.best_tier,
                result.best_tier,
                result.prompt_version,
                versions_hash(result.versions),
                result.paper_id,
            ),
        )
        for slug, v in verdicts.items():
            await conn.execute(
                """
                INSERT INTO verdicts (paper_id, selection, tier, confidence, reason, version,
                                      model, screened_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (paper_id, selection) DO UPDATE SET
                    tier = EXCLUDED.tier, confidence = EXCLUDED.confidence,
                    reason = EXCLUDED.reason, version = EXCLUDED.version,
                    model = EXCLUDED.model, screened_at = now()
                """,
                (
                    result.paper_id,
                    slug,
                    v.tier,
                    v.confidence.value,
                    v.reason,
                    result.versions.get(slug, ""),
                    result.model,
                ),
            )


def parse_screen(raw: dict[str, Any] | None) -> ScreenResult | None:
    """The stored screen JSON, or None for rows screened before selections existed."""
    if not raw or raw.get("schema_version") != 2:
        return None
    return ScreenResult.model_validate(raw)


async def store_screen_error(conn: psycopg.AsyncConnection, paper_id: str, error: str) -> None:
    """Leaves the paper pending so the next run retries it."""
    await conn.execute(
        "UPDATE papers SET screen_error = %s WHERE id = %s", (nul_free(error), paper_id)
    )


async def verdicts_for(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Verdict]:
    rows = await (
        await conn.execute(
            "SELECT selection, tier, confidence, reason FROM verdicts WHERE paper_id = %s",
            (paper_id,),
        )
    ).fetchall()
    return {
        r["selection"]: Verdict(tier=r["tier"], confidence=r["confidence"], reason=r["reason"])
        for r in rows
    }


async def unconfirmed_positives(
    conn: psycopg.AsyncConnection,
) -> list[tuple[Paper, ScreenResult]]:
    """Papers with a tier 1/2 verdict that no confirm stage has judged."""
    rows = await (
        await conn.execute(
            """
            SELECT p.* FROM papers p
            WHERE p.best_tier <= 2 AND p.screen->>'schema_version' = '2'
            ORDER BY p.published DESC
            """
        )
    ).fetchall()
    out = []
    for r in rows:
        result = parse_screen(r["screen"])
        if result and any(
            v.tier <= 2 and slug not in result.confirmed_slugs()
            for slug, v in result.verdicts.items()
        ):
            out.append((row_to_paper(r), result))
    return out


async def forget_verdicts(
    conn: psycopg.AsyncConnection, slug: str, window_days: int | None = None
) -> int:
    """Drop a selection's verdicts (in a window) so the next run judges them again."""
    since = datetime.now(UTC) - timedelta(days=window_days) if window_days is not None else None
    async with conn.transaction():
        locked = await (
            await conn.execute("SELECT pg_try_advisory_xact_lock(7213902) AS locked")
        ).fetchone()
        if not locked["locked"]:
            raise RuntimeError("cannot forget verdicts while a Litwatch run is in progress")
        row = await (
            await conn.execute(
                """
            WITH forgotten AS (
                DELETE FROM verdicts v USING papers p
                WHERE v.paper_id = p.id AND v.selection = %s
                  AND (%s::timestamptz IS NULL OR p.published >= %s)
                RETURNING v.paper_id
            ), invalidated AS (
                UPDATE inference_calls SET invalidated_at = clock_timestamp()
                WHERE paper_id IN (SELECT paper_id FROM forgotten) AND invalidated_at IS NULL
                RETURNING id
            ) SELECT count(*) AS count FROM forgotten
            """,
                (slug, since, since),
            )
        ).fetchone()
    return row["count"]


# -- selections -----------------------------------------------------------------------------------


async def list_selections(
    conn: psycopg.AsyncConnection, status: Status | None = None
) -> list[Selection]:
    clause = "WHERE status = %s" if status else ""
    rows = await (
        await conn.execute(
            f"SELECT * FROM selections {clause} ORDER BY created_at, slug",
            (status.value,) if status else (),
        )
    ).fetchall()
    return [_row_to_selection(r) for r in rows]


def _row_to_selection(r: dict[str, Any]) -> Selection:
    return Selection(
        slug=r["slug"],
        name=r["name"],
        subject=r["subject"],
        rubric=r["rubric"],
        keywords=list(r["keywords"] or []),
        owner=r["owner"],
        status=Status(r["status"]),
        digest=bool(r.get("digest")),
        notion_id=r["notion_id"],
    )


async def upsert_selection(conn: psycopg.AsyncConnection, s: Selection) -> bool:
    """Insert or update by slug. Returns True when the screener-visible content changed."""
    row = await (
        await conn.execute(
            """
            INSERT INTO selections (slug, name, owner, status, subject, rubric, keywords, version,
                                    digest, notion_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name, owner = EXCLUDED.owner, status = EXCLUDED.status,
                subject = EXCLUDED.subject, rubric = EXCLUDED.rubric,
                keywords = EXCLUDED.keywords, version = EXCLUDED.version,
                digest = EXCLUDED.digest,
                notion_id = coalesce(EXCLUDED.notion_id, selections.notion_id),
                updated_at = CASE WHEN selections.version = EXCLUDED.version
                                  THEN selections.updated_at ELSE now() END,
                synced_at = now()
            RETURNING (xmax = 0) AS inserted, (updated_at = synced_at) AS changed
            """,
            (
                s.slug,
                s.name,
                s.owner,
                s.status.value,
                s.subject,
                s.rubric,
                s.keywords,
                s.version,
                s.digest,
                s.notion_id,
            ),
        )
    ).fetchone()
    return bool(row and (row["inserted"] or row["changed"]))


async def pause_missing_selections(conn: psycopg.AsyncConnection, keep: list[str]) -> list[str]:
    """Selections absent from an authoritative sync are paused, never deleted.

    Their members' best tier is recomputed over the selections that stay active, so routing
    releases what only a paused selection wanted.
    """
    rows = await (
        await conn.execute(
            """
            UPDATE selections SET status = 'paused', synced_at = now()
            WHERE status <> 'paused' AND NOT (slug = ANY(%s)) RETURNING slug
            """,
            (keep,),
        )
    ).fetchall()
    paused = [r["slug"] for r in rows]
    if paused:
        await recompute_best_tier(conn, paused)
    return paused


async def recompute_best_tier(conn: psycopg.AsyncConnection, slugs: list[str]) -> int:
    """Best tier over active selections for every paper judged under one of `slugs`."""
    row = await (
        await conn.execute(
            """
            WITH affected AS (SELECT DISTINCT paper_id FROM verdicts WHERE selection = ANY(%s)),
            updated AS (
                UPDATE papers p SET best_tier = a.tier, demoted_at = {demoted},
                    -- No screening happened, so only this tells the projection the tier moved.
                    metadata_changed_at = CASE WHEN best_tier IS DISTINCT FROM a.tier
                        THEN clock_timestamp() ELSE metadata_changed_at END
                FROM (SELECT af.paper_id, coalesce(
                          (SELECT min(v.tier) FROM verdicts v
                           JOIN selections s ON s.slug = v.selection AND s.status = 'active'
                           WHERE v.paper_id = af.paper_id), 4) AS tier
                      FROM affected af) a
                WHERE p.id = a.paper_id AND p.best_tier IS NOT NULL
                RETURNING 1)
            SELECT count(*) AS n FROM updated
            """.format(demoted=DEMOTED_AT.format(new="a.tier")),
            (slugs,),
        )
    ).fetchone()
    return int(row["n"]) if row else 0


async def selection_counts(conn: psycopg.AsyncConnection) -> dict[str, dict[int, int]]:
    """Members per tier per selection, for the Notion status columns."""
    rows = await (
        await conn.execute(
            "SELECT selection, tier, count(*) AS n FROM verdicts WHERE tier < 4 GROUP BY 1, 2"
        )
    ).fetchall()
    out: dict[str, dict[int, int]] = {}
    for r in rows:
        out.setdefault(r["selection"], {})[int(r["tier"])] = int(r["n"])
    return out


# -- runs -----------------------------------------------------------------------------------------


async def start_run(
    conn: psycopg.AsyncConnection, kind: str, window_from: datetime, window_to: datetime
) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO runs (kind, window_from, window_to) VALUES (%s, %s, %s) RETURNING id",
            (kind, window_from, window_to),
        )
    ).fetchone()
    assert row is not None
    return int(row["id"])


async def finish_run(conn: psycopg.AsyncConnection, run_id: int, **fields: Any) -> None:
    # Error texts come from exceptions; one with a NUL must not leave the run unfinished.
    fields = nul_free(fields)
    if "tiers" in fields:
        fields["tiers"] = Jsonb(fields["tiers"])
    if "intake" in fields:
        fields["intake"] = Jsonb(fields["intake"])
    assignments = ", ".join(f"{k} = %({k})s" for k in fields)
    await conn.execute(
        f"UPDATE runs SET finished_at = now(), {assignments} WHERE id = %(id)s",
        {**fields, "id": run_id},
    )


async def get_run(conn: psycopg.AsyncConnection, run_id: int) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM runs WHERE id = %s", (run_id,))).fetchone()


async def list_runs(conn: psycopg.AsyncConnection, limit: int = 20) -> list[dict[str, Any]]:
    return await (
        await conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT %s", (limit,))
    ).fetchall()


async def store_digest(
    conn: psycopg.AsyncConnection,
    run_id: int,
    source: str,
    title: str,
    body: str,
    model: str | None,
    cost: float | None,
    *,
    tiers: dict[str, int] | None = None,
) -> None:
    """One digest per (run, source); rewriting replaces it."""
    await conn.execute(
        """
        INSERT INTO digests (run_id, source, title, body, model, cost, tiers)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (run_id, source) DO UPDATE SET
            title = EXCLUDED.title, body = EXCLUDED.body, model = EXCLUDED.model,
            cost = EXCLUDED.cost, tiers = EXCLUDED.tiers, created_at = now()
        """,
        (run_id, source, title, body, model, cost, Jsonb(tiers) if tiers is not None else None),
    )


async def list_digests(conn: psycopg.AsyncConnection, limit: int = 40) -> list[dict[str, Any]]:
    """Newest runs first, the general digest before the selection ones, with run facts."""
    return await (
        await conn.execute(
            """
            SELECT d.run_id, r.kind, r.started_at, r.finished_at, r.fetched, r.screened,
                   r.cost AS run_cost, r.tiers, r.intake, d.tiers AS digest_tiers,
                   d.source, d.title, d.body, d.model, d.cost
            FROM digests d JOIN runs r ON r.id = d.run_id
            ORDER BY d.run_id DESC, (d.source <> 'general'), d.title LIMIT %s
            """,
            (limit,),
        )
    ).fetchall()


# -- reading papers -------------------------------------------------------------------------------

VERDICTS_JSON = """
    (SELECT jsonb_object_agg(v.selection, jsonb_build_object(
                'tier', v.tier, 'confidence', v.confidence, 'reason', v.reason))
     FROM verdicts v JOIN selections s ON s.slug = v.selection AND s.status = 'active'
     WHERE v.paper_id = p.id AND v.tier < 4) AS verdicts
"""
PAPER_COLUMNS = """
    p.id, p.title, p.authors, p.categories, p.primary_category, p.published, p.url, p.fetched_at,
    p.version, p.screened_at, p.best_tier, p.prompt_version, p.selections_version,
    p.zotero_key, p.zotero_attachment_key, p.routed_at, p.routed_tier, p.route_error,
    p.enrichment, p.enriched_at, p.enrichment_model, p.enrichment_error, p.deep_stats
"""


async def list_papers(
    conn: psycopg.AsyncConnection,
    selection: str | None = None,
    tiers: list[int] | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Member papers (best tier 1..3), newest screening first, with verdicts, abstract, and state.

    `selection` restricts to members of that selection; `tiers` then applies to its verdict,
    otherwise to the paper's best tier.
    """
    tiers = tiers or [1, 2, 3]
    clauses, params = ["p.screened_at IS NOT NULL"], []
    if selection:
        clauses.append(
            "EXISTS (SELECT 1 FROM verdicts v WHERE v.paper_id = p.id AND v.selection = %s "
            "AND v.tier = ANY(%s))"
        )
        params += [selection, tiers]
    else:
        clauses.append("p.best_tier = ANY(%s)")
        params.append(tiers)
    order = "p.screened_at"
    if since:
        # "changed since": screening, routing, deep processing and shown metadata all matter to
        # the projection. Most recent changes first: under the limit, the cut rows are the oldest
        # changes, which the previous projection run already carried.
        order = "greatest(p.screened_at, p.routed_at, p.enriched_at, p.metadata_changed_at)"
        clauses.append(f"{order} >= %s")
        params.append(since)
    params.append(limit)
    return await (
        await conn.execute(
            f"""
            SELECT {PAPER_COLUMNS}, p.abstract, {VERDICTS_JSON}
            FROM papers p WHERE {" AND ".join(clauses)}
            ORDER BY {order} DESC LIMIT %s
            """,
            params,
        )
    ).fetchall()


# When a member row last changed in a way the projection shows.
CHANGED_AT = "greatest(p.screened_at, p.routed_at, p.enriched_at, p.metadata_changed_at)"
# A projection that has never acknowledged starts this far back, as the fixed window did.
PROJECTION_FIRST_WINDOW = timedelta(hours=26)


async def projection_cursor(conn: psycopg.AsyncConnection) -> tuple[datetime, str]:
    """The stored position; the first call fixes the starting point so a retry keeps it."""
    await conn.execute(
        "INSERT INTO projection_cursor (changed_at) VALUES (%s) ON CONFLICT (one) DO NOTHING",
        (datetime.now(UTC) - PROJECTION_FIRST_WINDOW,),
    )
    row = await (
        await conn.execute("SELECT changed_at, paper_id FROM projection_cursor")
    ).fetchone()
    return row["changed_at"], row["paper_id"]


async def _committed_before(conn: psycopg.AsyncConnection) -> datetime:
    """A time before which every change stamp is committed and visible.

    Stamps are never earlier than their transaction's start, so nothing invisible can be older
    than the oldest client transaction still open on this database (autovacuum and other
    background workers write no stamps). Read in its own statement: a
    transaction that ended before this read is visible to the query that follows.
    """
    row = await (
        await conn.execute(
            """
            SELECT least(now(), min(xact_start)) AS horizon FROM pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_backend_pid()
              AND xact_start IS NOT NULL AND backend_type = 'client backend'
            """
        )
    ).fetchone()
    return row["horizon"]


async def projection_changes(conn: psycopg.AsyncConnection, limit: int) -> dict[str, Any]:
    """The next member rows the projection has not acknowledged, oldest change first.

    `through` is the cursor to acknowledge once the rows are written; `more` says another call
    is due straight after. Nothing moves until `ack_projection`, so a failed projection is
    simply asked for the same rows again. Rows stamped while a writer is still open wait for
    the next call: committing behind the cursor, they would never be projected.
    """
    cursor = await projection_cursor(conn)
    horizon = await _committed_before(conn)
    rows = await (
        await conn.execute(
            f"""
            SELECT {PAPER_COLUMNS}, p.abstract, {VERDICTS_JSON}, {CHANGED_AT} AS changed_at
            FROM papers p
            WHERE p.screened_at IS NOT NULL AND p.best_tier < 4
              AND ({CHANGED_AT}, p.id) > (%s, %s) AND {CHANGED_AT} < %s
            ORDER BY {CHANGED_AT}, p.id LIMIT %s
            """,
            (*cursor, horizon, limit + 1),
        )
    ).fetchall()
    more, rows = len(rows) > limit, rows[:limit]
    through = (rows[-1]["changed_at"], rows[-1]["id"]) if rows else cursor
    return {
        "since": cursor[0],
        "papers": rows,
        "through": {"changed_at": through[0], "paper_id": through[1]},
        "more": more,
    }


async def ack_projection(
    conn: psycopg.AsyncConnection, changed_at: datetime, paper_id: str
) -> tuple[datetime, str]:
    """Move the cursor forward to an acknowledged position; it never moves back."""
    await conn.execute(
        """
        INSERT INTO projection_cursor (changed_at, paper_id) VALUES (%s, %s)
        ON CONFLICT (one) DO UPDATE
        SET changed_at = EXCLUDED.changed_at, paper_id = EXCLUDED.paper_id, updated_at = now()
        WHERE (projection_cursor.changed_at, projection_cursor.paper_id)
            < (EXCLUDED.changed_at, EXCLUDED.paper_id)
        """,
        (changed_at, paper_id),
    )
    return await projection_cursor(conn)


async def list_demoted(conn: psycopg.AsyncConnection, since: datetime) -> list[dict[str, Any]]:
    """Every paper that stopped being a member since `since` and still is not, latest first."""
    return await (
        await conn.execute(
            """
            SELECT id, title, demoted_at FROM papers
            WHERE demoted_at >= %s AND best_tier = 4
            ORDER BY demoted_at DESC, id
            """,
            (since,),
        )
    ).fetchall()


async def run_members(conn: psycopg.AsyncConnection, run_id: int) -> list[dict[str, Any]]:
    """Papers screened during the run that are members of some selection, for the digest."""
    return await (
        await conn.execute(
            f"""
            SELECT {PAPER_COLUMNS}, p.abstract, {VERDICTS_JSON}
            FROM papers p, runs r
            WHERE r.id = %s AND p.best_tier < 4
              AND p.screened_at >= r.started_at
              AND p.screened_at <= coalesce(r.finished_at, now())
            ORDER BY p.best_tier, p.published DESC
            """,
            (run_id,),
        )
    ).fetchall()


async def members_since(conn: psycopg.AsyncConnection, since: datetime) -> list[dict[str, Any]]:
    return await (
        await conn.execute(
            f"""
            SELECT {PAPER_COLUMNS}, p.abstract, {VERDICTS_JSON}
            FROM papers p WHERE p.best_tier < 4 AND p.screened_at >= %s
            ORDER BY p.best_tier, p.published DESC
            """,
            (since,),
        )
    ).fetchall()


async def stats(conn: psycopg.AsyncConnection) -> dict[str, Any]:
    tiers = await (
        await conn.execute(
            """
            SELECT CASE WHEN screened_at IS NULL THEN 'pending'
                        WHEN best_tier IS NULL THEN 'legacy'
                        ELSE best_tier::text END AS tier, count(*) AS n
            FROM papers GROUP BY 1
            """
        )
    ).fetchall()
    total = await (await conn.execute("SELECT count(*) AS n FROM papers")).fetchone()
    return {
        "papers": total["n"] if total else 0,
        "tiers": {r["tier"]: r["n"] for r in tiers},
        "selections": await selection_counts(conn),
    }


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)


# -- routing state --------------------------------------------------------------------------------


async def routable(
    conn: psycopg.AsyncConnection, paper_id: str | None = None, limit: int | None = None
) -> list[tuple[Paper, ScreenResult]]:
    """Snapshot of pending deliveries, tier 1 first; unlimited unless explicitly bounded.

    Route each snapshot once so a failed delivery cannot spin within the same run.
    PostgreSQL treats LIMIT NULL as unlimited.
    """
    clauses = [
        "best_tier <= 2",
        "screened_at IS NOT NULL",  # a rewritten revision waits for its new screen
        """(routed_at IS NULL
            OR routed_tier IS DISTINCT FROM best_tier::text
            OR (best_tier = 1 AND zotero_attachment_key IS NULL))""",
    ]
    params: list[Any] = []
    if paper_id:
        clauses.append("id = %s")
        params.append(paper_id)
    params.append(limit)
    rows = await (
        await conn.execute(
            f"SELECT * FROM papers WHERE {' AND '.join(clauses)} "
            "ORDER BY best_tier, published DESC NULLS LAST, id LIMIT %s",
            params,
        )
    ).fetchall()
    return [(row_to_paper(r), s) for r in rows if (s := parse_screen(r["screen"]))]


async def demoted(conn: psycopg.AsyncConnection, limit: int = 200) -> list[dict[str, Any]]:
    """Papers with a Zotero item that no longer earn one: routed, now below tier 2."""
    return await (
        await conn.execute(
            """
            SELECT id, zotero_key, routed_tier, best_tier FROM papers
            WHERE zotero_key IS NOT NULL AND routed_tier IS DISTINCT FROM 'none'
              AND best_tier IS NOT NULL AND best_tier > 2
            ORDER BY published DESC LIMIT %s
            """,
            (limit,),
        )
    ).fetchall()


async def store_route(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    tier: int | str,
    zotero_key: str | None,
    attachment_key: str | None,
) -> None:
    await conn.execute(
        """
        UPDATE papers SET routed_at = now(), routed_tier = %s,
                          zotero_key = coalesce(%s, zotero_key),
                          zotero_attachment_key = coalesce(%s, zotero_attachment_key),
                          route_error = NULL
        WHERE id = %s
        """,
        (str(tier), zotero_key, attachment_key, paper_id),
    )


async def store_route_error(conn: psycopg.AsyncConnection, paper_id: str, error: str) -> None:
    await conn.execute(
        "UPDATE papers SET route_error = %s WHERE id = %s", (nul_free(error), paper_id)
    )


async def get_paper(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    return await (await conn.execute("SELECT * FROM papers WHERE id = %s", (paper_id,))).fetchone()


# -- paper text and deep processing ---------------------------------------------------------------


async def store_body(conn: psycopg.AsyncConnection, paper_id: str, body: str, source: str) -> None:
    await conn.execute(
        "UPDATE papers SET body = %s, body_source = %s, body_fetched_at = now() WHERE id = %s",
        (nul_free(body), source, paper_id),
    )


async def deep_pending(
    conn: psycopg.AsyncConnection,
    paper_id: str | None = None,
    limit: int = 20,
    max_attempts: int = 3,
) -> list[dict[str, Any]]:
    """Tier 1 papers with a Zotero item and no enrichment yet. A named paper ignores attempts."""
    if paper_id:
        return await (
            await conn.execute("SELECT * FROM papers WHERE id = %s", (paper_id,))
        ).fetchall()
    return await (
        await conn.execute(
            """
            SELECT * FROM papers
            WHERE best_tier = 1 AND zotero_key IS NOT NULL AND enrichment IS NULL
              AND screened_at IS NOT NULL
              AND deep_attempts < %s
            ORDER BY published DESC LIMIT %s
            """,
            (max_attempts, limit),
        )
    ).fetchall()


async def zotero_enrichment_pending(
    conn: psycopg.AsyncConnection, paper_id: str | None = None
) -> list[dict[str, Any]]:
    """Enriched papers whose Zotero item has not received the note and tags yet."""
    clause = "AND id = %s" if paper_id else ""
    params: tuple[Any, ...] = (paper_id,) if paper_id else ()
    return await (
        await conn.execute(
            f"""
            SELECT id, title, abstract, authors, published, url, zotero_key, enrichment,
                   enrichment_model, deep_stats
            FROM papers
            WHERE enrichment IS NOT NULL AND zotero_key IS NOT NULL
              AND zotero_enriched_at IS NULL {clause}
            ORDER BY published DESC
            """,
            params,
        )
    ).fetchall()


async def store_enrichment(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    result: dict[str, Any],
    model: str,
    trace: list[dict[str, Any]],
    stats: dict[str, Any],
) -> None:
    await conn.execute(
        """
        UPDATE papers SET enrichment = %s, enriched_at = now(), enrichment_model = %s,
                          enrichment_error = NULL, deep_trace = %s, deep_stats = %s,
                          deep_attempts = deep_attempts + 1, zotero_enriched_at = NULL
        WHERE id = %s
        """,
        (
            Jsonb(nul_free(result)),
            nul_free(model),
            Jsonb(nul_free(trace)),
            Jsonb(nul_free(stats)),
            paper_id,
        ),
    )


async def store_enrichment_error(
    conn: psycopg.AsyncConnection,
    paper_id: str,
    error: str,
    trace: list[dict[str, Any]] | None = None,
    stats: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE papers SET enrichment_error = %s, deep_attempts = deep_attempts + 1,
                          deep_trace = coalesce(%s, deep_trace),
                          deep_stats = coalesce(%s, deep_stats)
        WHERE id = %s
        """,
        (
            nul_free(error),
            Jsonb(nul_free(trace)) if trace is not None else None,
            Jsonb(nul_free(stats)) if stats else None,
            paper_id,
        ),
    )


async def mark_zotero_enriched(conn: psycopg.AsyncConnection, paper_id: str) -> None:
    await conn.execute(
        "UPDATE papers SET zotero_enriched_at = now(), enrichment_error = NULL WHERE id = %s",
        (paper_id,),
    )


async def search_papers(
    conn: psycopg.AsyncConnection, query: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Full-text search over title and abstract for the deep processor's `store.search`."""
    return await (
        await conn.execute(
            f"""
            SELECT p.id, p.title, p.best_tier AS tier, p.published,
                   left(p.abstract, 400) AS abstract,
                   (SELECT v.reason FROM verdicts v WHERE v.paper_id = p.id AND v.tier < 4
                    ORDER BY v.tier LIMIT 1) AS reason,
                   {VERDICTS_JSON}
            FROM papers p, websearch_to_tsquery('english', %s) q
            WHERE p.search @@ q
            ORDER BY ts_rank(p.search, q) DESC, p.published DESC
            LIMIT %s
            """,
            (query, min(limit, 50)),
        )
    ).fetchall()


async def paper_summary(conn: psycopg.AsyncConnection, paper_id: str) -> dict[str, Any] | None:
    """One paper without the text and trace, which are big and served separately."""
    return await (
        await conn.execute(
            f"""
            SELECT {PAPER_COLUMNS}, p.abstract, p.screen, p.body_source, p.body_fetched_at,
                   length(p.body) AS body_chars, p.deep_attempts, p.zotero_enriched_at,
                   {VERDICTS_JSON}
            FROM papers p WHERE p.id = %s
            """,
            (paper_id,),
        )
    ).fetchone()
