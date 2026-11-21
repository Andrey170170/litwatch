"""Durable category harvesting with an announcement-feed fallback. Intake stores papers and
emits events; it judges nothing, so its success is independent of every later step."""

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from litwatch.archive import ArchiveClient
from litwatch.core import Paper
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate
from litwatch.intake import atom, batch, catalog, oai
from litwatch.intake.http import SourceError, XmlClient
from litwatch.intake.source import PaperSource
from litwatch.store import checkpoints, runs

log = logging.getLogger(__name__)


@dataclass
class IntakeResult:
    # The papers read, each once; of them, those new and those whose stored row changed.
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    # Stored papers whose title or abstract a revision rewrote, settled as they were stored.
    rewritten: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def unchanged(self) -> int:
        """The papers read that were neither new nor changed: most of a night's overlap."""
        return self.fetched - self.inserted - self.updated


async def _state(db: DatabaseGate, category: str, since: date, run_id: int) -> dict[str, Any]:
    """The category's checkpoint row, its first day persisted even if every source fails on
    the first attempt. A rolling three-day window must never erase an older unresolved gap."""
    async with db.write(f"intake: {category} checkpoint") as unit:
        previous = await runs.last_harvest_end(unit, run_id)
        initial = min(since, previous.date() - timedelta(days=1)) if previous else since
        return await checkpoints.opened(unit, category, initial, since)


async def _snapshots(db: DatabaseGate, category: str, since: date) -> list[atom.AtomBatch]:
    async with db.read(f"intake: {category} feeds") as unit:
        bodies = await checkpoints.feeds(unit, category, since)
    # Revalidate saved bytes. A corrupt archive cannot count as a healthy fallback.
    return [atom.parse_feed(body) for body in bodies]


async def collect(
    db: DatabaseGate,
    categories: tuple[str, ...],
    since: datetime,
    run_id: int,
    source: PaperSource = oai,
    archive: Callable[[], ArchiveClient] | None = None,
    *,
    rules: word.Rules,
) -> IntakeResult:
    """Store each complete category, then move its checkpoint; keep failed categories open.
    A category's papers commit a page at a time (`batch.store`, which settles a rewrite by
    `rules`) and its checkpoint only after the last, so an interrupted category is read again,
    and storing it again changes nothing.
    Given `archive` (what opens its archive client), the catalog pass (`intake.catalog`)
    follows, its outcome under `catalog` in the details; it leaves the status as the categories
    made it.

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
            state = await _state(db, category, since.date(), run_id)
            states[category] = state
            start = min(state["checkpoint"] or state["discovery_from"], since.date())
            info = {"from": start.isoformat(), "complete": False, "errors": []}
            result.details["categories"][category] = info
            try:
                feed = await asyncio.to_thread(atom.fetch_category, category, client)
                if feed.generated_at > datetime.now(UTC) + timedelta(minutes=5):
                    raise SourceError("Atom feed generation timestamp is in the future")
                async with db.write(f"intake: {category} feed") as unit:
                    await checkpoints.archive_feed(
                        unit,
                        category,
                        hashlib.sha256(feed.raw).hexdigest(),
                        feed.raw,
                        feed.generated_at,
                    )
                info["feed_dates"] = [d.isoformat() for d in feed.announcement_dates]
                info["feed_archived"] = True
            except SourceError as exc:
                info["errors"].append(f"Atom: {exc}")
                info["feed_archived"] = False
                log.warning("%s Atom unavailable: %s", category, exc)
            try:
                feeds[category] = await _snapshots(db, category, start)
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
            stored = await batch.store(db, unique, source="oai", rules=rules)
            async with db.write(f"intake: {category} checkpoint") as unit:
                await checkpoints.advance(unit, category, harvest.checkpoint.date(), run_id)
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
        stored = await batch.store(
            db, list(fallback.values()), source="atom", rules=rules, listed=listed
        )
        result.inserted += stored.inserted
        seen.update(fallback)
        if archive is not None:
            result.details["catalog"] = await catalog.run(
                db, archive, categories, since.date(), run_id, client, source=source
            )
    result.fetched = len(seen)
    if not all(info["complete"] for info in result.details["categories"].values()):
        result.details["status"] = "partial"
    async with db.write("intake: details") as unit:
        await runs.update(unit, run_id, intake=result.details)
    return result
