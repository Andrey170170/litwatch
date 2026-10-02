"""Digest eligibility and durable coverage, independent of mutable screening timestamps."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch import db
from litwatch.digest import (
    GENERAL,
    Digest,
    Digester,
    assign,
    by_tier,
    first_posted,
    render_revisions,
)
from litwatch.intake import coverage_notice
from litwatch.selection import Selection

log = logging.getLogger(__name__)
# Session lock shared by scheduled runs and CLI rewrites, including the model calls.
DIGEST_LOCK = 7213901


def canonical_url(url: str) -> str:
    """Match historical HTTP/HTTPS and versioned arXiv links to stored paper IDs."""
    match = re.fullmatch(r"https?://(?:www\.)?arxiv.org/(?:abs|pdf)/(.+?)(?:\.pdf)?", url)
    return "arxiv:" + re.sub(r"v\d+$", "", match[1]) if match else url


def normalized_title(text: str) -> str:
    """Only typographic normalization, never fuzzy matching of historical paper titles."""
    text = unicodedata.normalize("NFKC", text).translate(
        str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
    )
    return " ".join(text.casefold().split())


def historical_ids(body: str, papers: list[dict[str, Any]]) -> set[str]:
    """Recover explicit URLs and unambiguous, full-length titles from old prose."""
    by_url = {canonical_url(p["url"]): p["id"] for p in papers if p["url"]}
    by_url.update({"arxiv:" + p["id"]: p["id"] for p in papers})
    urls = re.findall(r"\]\(([^\s)]+)\)", body)
    ids = {by_url[canonical_url(u)] for u in urls if canonical_url(u) in by_url}
    titles: dict[str, list[str]] = {}
    for paper in papers:
        titles.setdefault(normalized_title(paper["title"]), []).append(paper["id"])
    text = normalized_title(body)
    for title, matches in titles.items():
        # Short/generic or duplicate titles cannot reliably identify an unlinked paper.
        if (
            len(title) >= 24
            and len(matches) == 1
            and title in text
            and re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", text)
        ):
            ids.add(matches[0])
    return ids


async def seed_history(conn: psycopg.AsyncConnection) -> None:
    """Import evidenced historical coverage once; old aggregate counts have no IDs."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (DIGEST_LOCK,))
        if await (
            await conn.execute(
                "SELECT 1 FROM digest_history_migrations WHERE name='paper-coverage-v1'"
            )
        ).fetchone():
            return
        papers = await (await conn.execute("SELECT id,url,title,version FROM papers")).fetchall()
        versions = {p["id"]: p["version"] for p in papers}
        selections = await db.list_selections(conn)
        labels = {s.name: s.slug for s in selections}
        section_pattern = (
            re.compile(r"(?m)^(" + "|".join(re.escape(name) for name in labels) + r"):\s+")
            if labels
            else None
        )
        rows = await (
            await conn.execute(
                "SELECT run_id,source,body,false AS legacy FROM digests UNION ALL "
                "SELECT id,'general',digest,true FROM runs WHERE digest IS NOT NULL ORDER BY run_id"
            )
        ).fetchall()
        for row in rows:
            sections = [(row["source"], row["body"])]
            if row["legacy"] and section_pattern:
                headings = list(section_pattern.finditer(row["body"]))
                for i, heading in enumerate(headings):
                    end = headings[i + 1].start() if i + 1 < len(headings) else len(row["body"])
                    sections.append((labels[heading[1]], row["body"][heading.end() : end]))
            for source, text in sections:
                for paper_id in historical_ids(text, papers):
                    await conn.execute(
                        "INSERT INTO digest_coverage (source,paper_id,run_id,version) "
                        "VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (source,paper_id) DO NOTHING",
                        (source, paper_id, row["run_id"], versions[paper_id]),
                    )
        await conn.execute(
            "INSERT INTO digest_history_migrations (name) VALUES ('paper-coverage-v1')"
        )


async def _batch(
    conn: psycopg.AsyncConnection,
    run_id: int,
    source: str,
    selections: list[Selection],
) -> dict[str, Any]:
    saved = await (
        await conn.execute(
            "SELECT snapshot FROM digest_batches WHERE run_id=%s AND source=%s", (run_id, source)
        )
    ).fetchone()
    if saved:
        snapshot = saved["snapshot"]
    else:
        run = await db.get_run(conn, run_id)
        assert run is not None
        cutoff = run["finished_at"] or datetime.now(UTC)
        # Include analysis from runs interrupted before a batch could be saved. The previous
        # issue's boundary is only a candidate watermark; coverage is the authority on repeats.
        members = await (
            await conn.execute(
                f"""
            SELECT {db.PAPER_COLUMNS}, p.abstract, {db.VERDICTS_JSON}
            FROM papers p, runs r
            WHERE r.id = %(run)s AND p.best_tier < 4
              AND p.screened_at >= least(r.started_at, coalesce(
                  (SELECT max(coalesce((b.snapshot->>'cutoff')::timestamptz,
                                       previous.finished_at, previous.started_at))
                   FROM digests d JOIN runs previous ON previous.id=d.run_id
                   LEFT JOIN digest_batches b ON b.run_id=d.run_id AND b.source=d.source
                   WHERE d.source=%(source)s AND d.run_id<r.id),
                  (SELECT created_at FROM digest_history_migrations
                   WHERE name='paper-coverage-v1')))
              AND p.screened_at <= %(cutoff)s
            ORDER BY p.best_tier, p.published DESC, p.id
            """,
                {"run": run_id, "source": source, "cutoff": cutoff},
            )
        ).fetchall()
        # Failed generation from an earlier run remains eligible even without re-screening.
        pending = await (
            await conn.execute(
                "SELECT b.snapshot FROM digest_batches b WHERE b.source=%s AND b.run_id<%s "
                "AND NOT EXISTS (SELECT 1 FROM digests d "
                "WHERE d.run_id=b.run_id AND d.source=b.source) "
                "ORDER BY b.run_id",
                (source, run_id),
            )
        ).fetchall()
        pending_ids = list({p["id"] for b in pending for p in b["snapshot"]["papers"]})
        # A new issue uses current judgments/cards, including demotions and removed verdicts.
        # Only regeneration of the same issue preserves the old snapshot.
        refreshed = (
            await (
                await conn.execute(
                    f"SELECT {db.PAPER_COLUMNS}, p.abstract, {db.VERDICTS_JSON} "
                    "FROM papers p WHERE p.id = ANY(%s)",
                    (pending_ids,),
                )
            ).fetchall()
            if pending_ids
            else []
        )
        papers = {p["id"]: p for p in refreshed}
        papers.update({p["id"]: p for p in members})
        # Membership is per stream; the general issue includes all active selections.
        slugs = {s.slug for s in selections} if source == GENERAL else {source}
        papers = {
            id: p
            for id, p in papers.items()
            if any(slug in slugs and v["tier"] < 4 for slug, v in (p.get("verdicts") or {}).items())
        }
        for p in papers.values():
            p["first_posted"] = first_posted(p)
        snapshot = {
            "cutoff": cutoff.isoformat(),
            "selections": [s.model_dump(mode="json") for s in selections],
            "papers": list(papers.values()),
            "revisions": await _revisions(conn, run_id, source, slugs),
        }
    covered = await (
        await conn.execute(
            "SELECT paper_id FROM digest_coverage WHERE source=%s AND run_id<>%s", (source, run_id)
        )
    ).fetchall()
    excluded = {r["paper_id"] for r in covered}
    noted = await (
        await conn.execute(
            "SELECT paper_id, version FROM digest_revisions WHERE source=%s AND run_id<>%s",
            (source, run_id),
        )
    ).fetchall()
    taken = {(r["paper_id"], r["version"]) for r in noted}
    return {
        **snapshot,
        "papers": [p for p in snapshot["papers"] if p["id"] not in excluded],
        "revisions": [
            r for r in snapshot.get("revisions", []) if (r["id"], r["version"]) not in taken
        ],
    }


async def _revisions(
    conn: psycopg.AsyncConnection, run_id: int, source: str, slugs: set[str]
) -> list[dict[str, Any]]:
    """Papers this stream covered at an older version, tier 1 or 2 here now, not yet noted."""
    rows = await (
        await conn.execute(
            f"""
            SELECT p.id, p.title, p.url, p.version, c.version AS covered_version,
                   r.started_at AS covered_at, {db.VERDICTS_JSON}
            FROM digest_coverage c JOIN papers p ON p.id = c.paper_id JOIN runs r ON r.id = c.run_id
            WHERE c.source = %(source)s AND c.run_id <> %(run)s AND p.version > c.version
              AND p.screened_at IS NOT NULL  -- a rewritten revision waits for its new screen
              AND NOT EXISTS (SELECT 1 FROM digest_revisions d WHERE d.source = c.source
                              AND d.paper_id = c.paper_id AND d.version = p.version)
            ORDER BY p.best_tier, p.id
            """,
            {"source": source, "run": run_id},
        )
    ).fetchall()
    return [
        r
        for r in rows
        if any(slug in slugs and v["tier"] <= 2 for slug, v in (r["verdicts"] or {}).items())
    ]


async def write_run_digests(
    conn: psycopg.AsyncConnection,
    digester: Digester,
    selections: list[Selection],
    run_id: int,
    *,
    store: bool = False,
) -> list[Digest]:
    """Newly analyzed, never-covered papers per source; previews do not change state.

    Successful bodies and coverage commit together. Membership includes count-only papers:
    a tier promotion or subsequent deep card is an update, not another new-paper announcement.
    A newer arXiv version of a covered tier 1/2 paper gets one dated note per stream and version.
    """
    locked = await (
        await conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (DIGEST_LOCK,))
    ).fetchone()
    if not locked or not locked["locked"]:
        raise RuntimeError("another digest generation is in progress; retry after it finishes")
    try:
        run = await db.get_run(conn, run_id)
        if not run:
            raise ValueError(f"unknown run {run_id}")
        notice = coverage_notice(run.get("intake"))
        sources = list(dict.fromkeys([GENERAL, *(s.slug for s in selections if s.digest)]))
        saved = await (
            await conn.execute(
                "SELECT source FROM digest_batches WHERE run_id=%s ORDER BY source", (run_id,)
            )
        ).fetchall()
        if saved:
            sources = [r["source"] for r in saved]
            sources.sort(key=lambda source: source != GENERAL)
        # Freeze every source before calling a model, so interruption preserves pending work.
        batches = {}
        for source in sources:
            batch = await _batch(conn, run_id, source, selections)
            if source != GENERAL and not batch["papers"] and not batch["revisions"]:
                continue
            batches[source] = batch
        if store:
            # All sources or none: a crash cannot leave a partial source list on retry.
            async with conn.transaction():
                # Coverage of a then-unknown version adopts the first known one as baseline.
                await conn.execute(
                    "UPDATE digest_coverage c SET version = p.version FROM papers p "
                    "WHERE p.id = c.paper_id AND c.version IS NULL AND p.version IS NOT NULL"
                )
                for source, batch in batches.items():
                    await conn.execute(
                        "INSERT INTO digest_batches (run_id,source,snapshot) VALUES (%s,%s,%s) "
                        "ON CONFLICT (run_id,source) DO UPDATE SET snapshot=EXCLUDED.snapshot",
                        (run_id, source, Jsonb(json.loads(db.dumps(batch)))),
                    )
        results = []
        for source, batch in batches.items():
            specs = [Selection.model_validate(s) for s in batch["selections"]]
            try:
                if source == GENERAL:
                    owned = assign(specs, batch["papers"])
                    counts = {str(t): sum(len(g[t]) for g in owned.values()) for t in (1, 2, 3)}
                    digest = await digester.write_general(specs, owned)
                else:
                    selection = next(s for s in specs if s.slug == source)
                    groups = by_tier(selection, batch["papers"])
                    counts = {str(t): len(groups[t]) for t in (1, 2, 3)}
                    digest = await digester.write_selection(selection, groups)
            except Exception:
                log.exception("digest %s failed; its papers remain pending", source)
                continue
            if batch["revisions"]:
                digest.body += "\n\n" + render_revisions(specs, batch["revisions"], source)
            if notice:
                digest.body = notice + "\n\n" + digest.body
            if store:
                async with conn.transaction():
                    await db.store_digest(
                        conn,
                        run_id,
                        source,
                        digest.title,
                        digest.body,
                        digest.model,
                        digest.cost,
                        tiers=counts,
                    )
                    for p in batch["papers"]:
                        await conn.execute(
                            "INSERT INTO digest_coverage (source,paper_id,run_id,version) "
                            "VALUES (%s,%s,%s,%s) "
                            "ON CONFLICT (source,paper_id) DO NOTHING",
                            (source, p["id"], run_id, p.get("version")),
                        )
                    for r in batch["revisions"]:
                        await conn.execute(
                            "INSERT INTO digest_revisions (source,paper_id,version,run_id) "
                            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (source, r["id"], r["version"], run_id),
                        )
            results.append(digest)
        return results
    finally:
        await conn.execute("SELECT pg_advisory_unlock(%s)", (DIGEST_LOCK,))
