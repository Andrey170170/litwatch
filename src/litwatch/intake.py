"""Durable category harvesting and announcement fallback, separate from analysis success."""

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch import archive_outbox, atom_source, db, oai_source
from litwatch.archive_outbox import ArchivePolicy
from litwatch.models import Paper
from litwatch.source_http import SourceError, XmlClient

log = logging.getLogger(__name__)


@dataclass
class IntakeResult:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    details: dict[str, Any] = field(default_factory=dict)


def coverage_notice(details: dict[str, Any] | None) -> str:
    """Reader-facing warning; never present missing intake as a quiet announcement day."""
    if details and details.get("status") == "skipped":
        return "**No new intake attempted.** This issue analyzes papers already in the library."
    if not details or details.get("status") != "partial":
        return ""
    gaps = [
        f"{category} (since {info['from']})"
        for category, info in details.get("categories", {}).items()
        if not info.get("complete")
    ]
    return (
        "**Incomplete paper intake.** This issue covers the papers available for analysis. "
        "Missing coverage will be retried: " + ", ".join(gaps) + "."
    )


async def _state(conn, category: str, since: date, run_id: int) -> dict[str, Any]:
    # Persist the initial lower bound even if every source fails on the first attempt.
    # A rolling three-day window must never erase an older unresolved gap.
    previous = await (
        await conn.execute(
            "SELECT window_to FROM runs WHERE id<%s AND finished_at IS NOT NULL "
            "AND error IS NULL AND fetched>0 "
            "AND (intake IS NULL OR intake->>'status'='complete') ORDER BY id DESC LIMIT 1",
            (run_id,),
        )
    ).fetchone()
    initial = min(since, previous["window_to"].date() - timedelta(days=1)) if previous else since
    await conn.execute(
        "INSERT INTO intake_checkpoints(category,discovery_from) VALUES (%s,%s) "
        "ON CONFLICT(category) DO UPDATE SET "
        "discovery_from=least(intake_checkpoints.discovery_from,%s)",
        (category, initial, since),
    )
    return await (
        await conn.execute("SELECT * FROM intake_checkpoints WHERE category=%s", (category,))
    ).fetchone()


async def _snapshots(conn, category: str, since: date) -> list[atom_source.AtomBatch]:
    rows = await (
        await conn.execute(
            "SELECT body FROM intake_snapshots WHERE category=%s AND generated_at >= %s "
            "ORDER BY generated_at",
            (category, datetime.combine(since, datetime.min.time(), UTC)),
        )
    ).fetchall()
    # Revalidate saved bytes. A corrupt archive cannot count as a healthy fallback.
    return [atom_source.parse_feed(bytes(row["body"])) for row in rows]


async def collect(
    conn: psycopg.AsyncConnection,
    categories: tuple[str, ...],
    since: datetime,
    run_id: int,
    *,
    archive_policy: ArchivePolicy | None = None,
) -> IntakeResult:
    """Commit each complete category and checkpoint together; keep failed categories open.

    Feeds are archived before OAI harvesting so today's announcements survive even
    an interrupted harvest. Only new/cross announcements insert fallback candidates.
    OAI admits all changed records: an old creation date may be a new cross-listing,
    so filtering it out would silently lose coverage after the daily feed rotates.
    """
    result = IntakeResult(details={"status": "complete", "categories": {}})
    states, feeds = {}, {}
    seen: set[str] = set()
    seen_oai: set[str] = set()
    with XmlClient() as client:
        for category in dict.fromkeys(categories):
            state = await _state(conn, category, since.date(), run_id)
            states[category] = state
            start = min(state["checkpoint"] or state["discovery_from"], since.date())
            if archive_policy is not None and category in archive_policy.categories:
                await conn.execute(
                    "INSERT INTO archive_intake_checkpoints(category,policy,discovery_from) "
                    "VALUES(%s,%s,%s) ON CONFLICT(category) DO NOTHING",
                    (category, archive_policy.name, start),
                )
                archival = await (
                    await conn.execute(
                        "SELECT * FROM archive_intake_checkpoints WHERE category=%s", (category,)
                    )
                ).fetchone()
                if archival["policy"] != archive_policy.name:
                    raise ValueError("Archive policy changed: establish new coverage explicitly")
                start = min(start, archival["checkpoint"] or archival["discovery_from"])
            info = {"from": start.isoformat(), "complete": False, "errors": []}
            result.details["categories"][category] = info
            try:
                feed = await asyncio.to_thread(atom_source.fetch_category, category, client)
                if feed.generated_at > datetime.now(UTC) + timedelta(minutes=5):
                    raise SourceError("Atom feed generation timestamp is in the future")
                await conn.execute(
                    "INSERT INTO intake_snapshots(category,sha256,body,generated_at) "
                    "VALUES(%s,%s,%s,%s) ON CONFLICT(category,sha256) DO NOTHING",
                    (category, hashlib.sha256(feed.raw).hexdigest(), feed.raw, feed.generated_at),
                )
                info["feed_dates"] = [d.isoformat() for d in feed.announcement_dates]
                info["feed_archived"] = True
            except SourceError as exc:
                info["errors"].append(f"Atom: {exc}")
                info["feed_archived"] = False
                log.warning("%s Atom unavailable: %s", category, exc)
            try:
                feeds[category] = await _snapshots(conn, category, start)
            except SourceError as exc:
                info["errors"].append(f"Atom archive: {exc}")
                feeds[category] = []

        for category in states:
            info = result.details["categories"][category]
            try:
                batch = await asyncio.to_thread(
                    oai_source.fetch_category,
                    category,
                    date.fromisoformat(info["from"]),
                    client,
                    **({"include_versions": True} if archive_policy is not None else {}),
                )
                if batch.checkpoint.date() < date.fromisoformat(info["from"]):
                    raise SourceError("OAI response predates the requested coverage")
                if batch.checkpoint > datetime.now(UTC) + timedelta(minutes=5):
                    raise SourceError("OAI response timestamp is in the future")
            except SourceError as exc:
                info["errors"].append(f"OAI: {exc}")
                log.warning("%s OAI unavailable: %s", category, exc)
                continue
            papers = batch.papers
            unique = [p for p in papers if p.id not in seen_oai]
            async with conn.transaction():
                inserted, updated = await db.upsert_papers(
                    conn,
                    unique,
                    source="oai",
                    archive_policy=archive_policy,
                    archive_run_id=run_id,
                )
                await conn.execute(
                    "UPDATE intake_checkpoints SET checkpoint=greatest(checkpoint,%s), "
                    "checked_at=now(),run_id=%s WHERE category=%s",
                    (batch.checkpoint.date(), run_id, category),
                )
                if archive_policy is not None and category in archive_policy.categories:
                    await conn.execute(
                        "UPDATE archive_intake_checkpoints SET checkpoint=greatest(checkpoint,%s), "
                        "checked_at=now(),run_id=%s WHERE category=%s",
                        (batch.checkpoint.date(), run_id, category),
                    )
                    info["archive_versions"] = {
                        "status": "partial" if batch.versions_error else "complete",
                        "error": batch.versions_error,
                        "unknown": sum(p.version is None for p in papers),
                    }
            result.inserted += inserted
            result.updated += updated
            seen_oai.update(p.id for p in papers)
            seen.update(p.id for p in papers)
            info.update(
                complete=True,
                through=batch.checkpoint.isoformat(),
                source="oai",
                records=len(batch.papers),
                admitted=len(papers),
            )

        # Canonical records win over lower-fidelity feeds, including across categories.
        fallback: dict[str, Paper] = {}
        archival_fallback: dict[tuple[str, int | None], tuple[Paper, str]] = {}
        for category, batches in feeds.items():
            info = result.details["categories"][category]
            for batch in batches:
                for p in batch.papers:
                    archival_fallback[(p.id, p.version)] = (p, batch.generated_at.isoformat())
                    if batch.announcements[p.id] in ("new", "cross"):
                        fallback[p.id] = p
            if not info["complete"]:
                info["source"] = (
                    "atom"
                    if info.get("feed_archived")
                    else ("atom-cache" if batches else "unavailable")
                )
                info["cached_feed_dates"] = sorted(
                    {d.isoformat() for batch in batches for d in batch.announcement_dates}
                )
        async with conn.transaction():
            inserted, _ = await db.upsert_papers(conn, list(fallback.values()), source="atom")
            if archive_policy is not None:
                for paper, observed_at in archival_fallback.values():
                    await archive_outbox.enqueue(
                        conn,
                        [paper],
                        archive_policy,
                        source="atom",
                        run_id=run_id,
                        observed_at=observed_at,
                    )
        result.inserted += inserted
        seen.update(fallback)
    result.fetched = len(seen)
    if not all(info["complete"] for info in result.details["categories"].values()):
        result.details["status"] = "partial"
    await conn.execute("UPDATE runs SET intake=%s WHERE id=%s", (Jsonb(result.details), run_id))
    return result


async def record_skipped(conn: psycopg.AsyncConnection, run_id: int) -> dict[str, Any]:
    """Processing-only retries inherit unresolved gaps rather than hiding them."""
    previous = await (
        await conn.execute(
            "SELECT intake FROM runs WHERE id<%s AND intake IS NOT NULL ORDER BY id DESC LIMIT 1",
            (run_id,),
        )
    ).fetchone()
    details = {"status": "skipped"}
    if previous and previous["intake"].get("status") == "partial":
        details = {**previous["intake"], "fetch_skipped": True}
    await conn.execute("UPDATE runs SET intake=%s WHERE id=%s", (Jsonb(details), run_id))
    return details
