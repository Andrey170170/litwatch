"""Deliver durable archival intents independently of relevance processing.

Remote job acceptance is not file availability. Failed remote jobs remain visible
and are checked slowly until an operator retries them in the archive service.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from psycopg.types.json import Jsonb

from litwatch import archive_outbox, db
from litwatch.archive_client import ArchiveClient, ArchiveVersionUnknown
from litwatch.models import Paper
from litwatch.oai_source import fetch_versions, resolve_observed_version
from litwatch.source_http import SourceError, XmlClient

log = logging.getLogger(__name__)


def _retry_delay(attempts: int) -> int:
    return min(3600, 30 * 2 ** min(max(attempts - 1, 0), 7))


def _observed_version(
    paper: Paper, history: dict, source: str, observed_at: str | None = None
) -> Paper:
    """Translate the shared observation resolver's failure into an archive outcome."""
    try:
        return resolve_observed_version(paper, history, source, observed_at=observed_at)
    except SourceError as exc:
        raise ArchiveVersionUnknown(str(exc)) from exc


def _fetch_history(paper_id: str) -> dict:
    with XmlClient() as client:
        return fetch_versions(paper_id, client)


async def _resolve(conn, client: ArchiveClient, row: dict, paper: Paper) -> Paper:
    if paper.version is not None:
        return paper
    try:
        paper = await client.resolve(paper)
    except ArchiveVersionUnknown:
        history = await asyncio.to_thread(_fetch_history, paper.id)
        observed_at = row["payload"].get("observed_at")
        if observed_at is None and row.get("created_at") is not None:
            observed_at = row["created_at"].isoformat()
        paper = _observed_version(paper, history, row["payload"].get("source", ""), observed_at)
    if row["payload"].get("source") != "atom":
        # The earlier unknown-version metadata event may have been rejected as
        # older than a known catalog revision. Publish its now-explicit revision
        # before persisting resolution, so a failed publication remains retryable.
        await client.upsert_paper(paper)
    payload = {**row["payload"], "paper": paper.model_dump(mode="json")}
    resolved = await (
        await conn.execute(
            """UPDATE archive_outbox SET version=%s,payload=%s,updated_at=now(),
                   lease_until=now()+interval '300 seconds'
               WHERE id=%s AND lease_token=%s AND lease_until>now() RETURNING id""",
            (paper.version, Jsonb(payload), row["id"], row["lease_token"]),
        )
    ).fetchone()
    if resolved is None:
        raise RuntimeError("archive delivery lease expired during revision resolution")
    return paper


async def deliver_once(conn, client: ArchiveClient, limit: int = 1) -> int:
    """Deliver at most limit intents, claiming each immediately before its work."""
    if limit < 1:
        raise ValueError("delivery limit must be positive")
    processed = 0
    for _ in range(limit):
        rows = await archive_outbox.claim(conn, limit=1, lease_seconds=300)
        if not rows:
            break
        row = rows[0]
        remote_id = row.get("remote_job_id")
        status, error, retry = "error", None, _retry_delay(row["attempts"])
        try:
            paper = Paper.model_validate(row["payload"]["paper"])
            if row["kind"] == "metadata":
                await client.upsert_paper(paper)
                status = "available"
            else:
                paper = await _resolve(conn, client, row, paper)
                # Re-requesting promotes an accepted job without duplicating it.
                if remote_id is None or row["priority"] == 0:
                    job = await client.request(
                        paper, kind=row["kind"], priority=row["priority"], publish_metadata=False
                    )
                    remote_id = job["id"]
                else:
                    job = await client.job(remote_id)
                remote_status = job.get("status")
                if remote_status == "complete":
                    status = "available"
                elif remote_status == "unavailable":
                    status = "unavailable"
                elif remote_status == "failed":
                    status, retry = "error", 3600
                    error = "archive job failed; explicit archive retry required"
                elif remote_status in ("pending", "running"):
                    status, retry = "delivered", 30 if row["priority"] == 0 else 120
                else:
                    raise ValueError("invalid remote archive job state")
        except Exception as exc:
            # Exception messages can contain URLs or server bodies; record type
            # only. Remote job failures have an explicit safe message above.
            error = f"archive delivery failed ({type(exc).__name__})"
            log.warning("Archive delivery row=%s failed (%s)", row["id"], type(exc).__name__)
        acknowledged = await archive_outbox.complete(
            conn,
            row["id"],
            row["lease_token"],
            status=status,
            remote_job_id=remote_id,
            error=error,
            retry_seconds=retry,
        )
        if not acknowledged:
            log.warning("Archive delivery row=%s lost its lease", row["id"])
        processed += 1
    return processed


async def run_dispatcher(
    dsn: str,
    client: ArchiveClient,
    stop: asyncio.Event,
    interval_seconds: float = 5,
) -> None:
    """Own one PostgreSQL connection; reconnect on faults and stop cancellably."""
    if interval_seconds <= 0:
        raise ValueError("dispatcher interval must be positive")
    conn = None
    try:
        while not stop.is_set():
            worked = False
            try:
                if conn is None or conn.closed:
                    conn = await db.connect(dsn)
                worked = bool(await deliver_once(conn, client))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Archive dispatcher unavailable (%s)", type(exc).__name__)
                if conn is not None:
                    with suppress(Exception):
                        await conn.close()
                    conn = None
            if worked:
                # Yield to curated work; there is no need to add a five-second
                # delay between local API deliveries while there is a backlog.
                await asyncio.sleep(0)
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
    finally:
        if conn is not None:
            await conn.close()
