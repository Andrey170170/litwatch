"""Durable category harvesting with an announcement-feed fallback. Intake stores papers and
emits events; it judges nothing, so its success is independent of every later step."""

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch.core import Paper
from litwatch.intake import atom, batch, oai
from litwatch.intake.http import SourceError, XmlClient
from litwatch.intake.source import PaperSource

log = logging.getLogger(__name__)


@dataclass
class IntakeResult:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    # Stored papers whose title or abstract a revision rewrote, settled as they were stored.
    rewritten: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


async def _state(conn, category: str, since: date, run_id: int) -> dict[str, Any]:
    # Persist the initial lower bound even if every source fails on the first attempt.
    # A rolling three-day window must never erase an older unresolved gap. A read from the
    # archive covers new papers only, not every change, so it is no harvest to go on from.
    previous = await (
        await conn.execute(
            "SELECT window_to FROM runs WHERE id<%s AND finished_at IS NOT NULL "
            "AND error IS NULL AND fetched>0 AND (intake IS NULL OR "
            "(intake->>'status'='complete' AND intake->>'source' IS DISTINCT FROM 'archive')) "
            "ORDER BY id DESC LIMIT 1",
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


async def _snapshots(conn, category: str, since: date) -> list[atom.AtomBatch]:
    rows = await (
        await conn.execute(
            "SELECT body FROM intake_snapshots WHERE category=%s AND generated_at >= %s "
            "ORDER BY generated_at",
            (category, datetime.combine(since, datetime.min.time(), UTC)),
        )
    ).fetchall()
    # Revalidate saved bytes. A corrupt archive cannot count as a healthy fallback.
    return [atom.parse_feed(bytes(row["body"])) for row in rows]


async def collect(
    conn: psycopg.AsyncConnection,
    categories: tuple[str, ...],
    since: datetime,
    run_id: int,
    source: PaperSource = oai,
) -> IntakeResult:
    """Store each complete category, then move its checkpoint; keep failed categories open.
    A category's papers commit a page at a time (`batch.store`) and its checkpoint only after
    the last, so an interrupted category is read again, and storing it again changes nothing.

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
            info = {"from": start.isoformat(), "complete": False, "errors": []}
            result.details["categories"][category] = info
            try:
                feed = await asyncio.to_thread(atom.fetch_category, category, client)
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
                harvest = await asyncio.to_thread(
                    source.fetch_category,
                    category,
                    date.fromisoformat(info["from"]),
                    client,
                    # Records name the paper version, and v1 is the true first posting.
                    include_versions=True,
                )
                if harvest.checkpoint.date() < date.fromisoformat(info["from"]):
                    raise SourceError("OAI response predates the requested coverage")
                if harvest.checkpoint > datetime.now(UTC) + timedelta(minutes=5):
                    raise SourceError("OAI response timestamp is in the future")
            except SourceError as exc:
                info["errors"].append(f"OAI: {exc}")
                log.warning("%s OAI unavailable: %s", category, exc)
                continue
            papers = harvest.papers
            unique = [p for p in papers if p.id not in seen_oai]
            stored = await batch.store(conn, unique, source="oai")
            await conn.execute(
                "UPDATE intake_checkpoints SET checkpoint=greatest(checkpoint,%s), "
                "checked_at=now(),run_id=%s WHERE category=%s",
                (harvest.checkpoint.date(), run_id, category),
            )
            if harvest.versions_error:
                info["versions_error"] = harvest.versions_error
            result.inserted += stored.inserted
            result.updated += stored.updated
            result.rewritten += stored.rewritten
            seen_oai.update(p.id for p in papers)
            seen.update(p.id for p in papers)
            info.update(
                complete=True,
                through=harvest.checkpoint.isoformat(),
                source="oai",
                records=len(harvest.papers),
                admitted=len(papers),
            )

        # Canonical records win over lower-fidelity feeds, including across categories.
        fallback: dict[str, Paper] = {}
        listed: dict[str, date] = {}
        for category, batches in feeds.items():
            info = result.details["categories"][category]
            for snapshot in batches:
                for p in snapshot.papers:
                    if snapshot.announcements[p.id] in ("new", "cross"):
                        fallback[p.id] = p
                        if p.id in snapshot.listed:
                            listed[p.id] = snapshot.listed[p.id]
            if not info["complete"]:
                info["source"] = (
                    "atom"
                    if info.get("feed_archived")
                    else ("atom-cache" if batches else "unavailable")
                )
                info["cached_feed_dates"] = sorted(
                    {d.isoformat() for snapshot in batches for d in snapshot.announcement_dates}
                )
        stored = await batch.store(conn, list(fallback.values()), source="atom", listed=listed)
        result.inserted += stored.inserted
        seen.update(fallback)
    result.fetched = len(seen)
    if not all(info["complete"] for info in result.details["categories"].values()):
        result.details["status"] = "partial"
    await conn.execute("UPDATE runs SET intake=%s WHERE id=%s", (Jsonb(result.details), run_id))
    return result
