"""Durable archival intents, independent of relevance and remote service availability."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from psycopg.types.json import Jsonb

from litwatch.models import Paper


@dataclass(frozen=True)
class ArchivePolicy:
    categories: tuple[str, ...]
    artifacts: tuple[str, ...] = ("pdf", "source")
    name: str = "cs-ai-v1"

    def accepts(self, paper: Paper) -> bool:
        return bool(set(self.categories).intersection(paper.categories))


async def enqueue(
    conn,
    papers: list[Paper],
    policy: ArchivePolicy,
    *,
    source: str,
    run_id: int | None = None,
    priority: int = 100,
    observed_at: str | None = None,
) -> int:
    """Called inside the intake transaction; outbox rows do not depend on curated membership.

    Metadata observations have content identity; artifacts have version identity. An
    unknown revision remains actionable work for the dispatcher, never an assumed v1.
    """
    count = 0
    for paper in papers:
        if not policy.accepts(paper):
            continue
        payload = {"paper": paper.model_dump(mode="json"), "source": source, "policy": policy.name}
        if observed_at is not None:
            payload["observed_at"] = observed_at
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        kinds = ["metadata", *policy.artifacts]
        if source == "atom":
            current = await (
                await conn.execute("SELECT metadata_source FROM papers WHERE id=%s", (paper.id,))
            ).fetchone()
            if current and current["metadata_source"] != "atom":
                kinds.remove("metadata")
        for kind in kinds:
            if kind not in ("metadata", "pdf", "source"):
                raise ValueError(f"Unsupported archive artifact: {kind}")
            identity = (
                digest if kind == "metadata" else (str(paper.version) if paper.version else digest)
            )
            event_key = f"{paper.id}:{kind}:{identity}"
            row = await (
                await conn.execute(
                    """INSERT INTO archive_outbox
                   (event_key,paper_id,version,kind,payload,priority,run_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(event_key) DO UPDATE SET
                     priority=least(archive_outbox.priority,EXCLUDED.priority)
                   RETURNING (xmax=0) AS inserted""",
                    (event_key, paper.id, paper.version, kind, Jsonb(payload), priority, run_id),
                )
            ).fetchone()
            count += int(row["inserted"])
    return count


async def claim(conn, *, limit: int = 10, lease_seconds: int = 300) -> list[dict]:
    """Claim due deliveries or accepted jobs needing reconciliation, safely across workers."""
    token = uuid4().hex
    return await (
        await conn.execute(
            """WITH due AS (
             SELECT id FROM archive_outbox
             WHERE status IN ('pending','error','delivered') AND next_attempt_at<=now()
               AND (lease_until IS NULL OR lease_until<=now())
             ORDER BY priority,id FOR UPDATE SKIP LOCKED LIMIT %s
           ) UPDATE archive_outbox o SET lease_token=%s,
             lease_until=now()+make_interval(secs=>%s),attempts=attempts+1,updated_at=now()
             FROM due WHERE o.id=due.id RETURNING o.*""",
            (limit, token, lease_seconds),
        )
    ).fetchall()


async def complete(
    conn,
    row_id: int,
    lease_token: str,
    *,
    status: str,
    remote_job_id: int | str | None = None,
    error: str | None = None,
    retry_seconds: int = 60,
) -> bool:
    """Only the current lease owner can acknowledge a result; acceptance is not availability."""
    if status not in ("pending", "delivered", "available", "unavailable", "error"):
        raise ValueError(f"Unsupported archive state: {status}")
    row = await (
        await conn.execute(
            """UPDATE archive_outbox SET status=%s,
             remote_job_id=coalesce(%s,remote_job_id),error=%s,
             next_attempt_at=now()+make_interval(secs=>%s),lease_token=NULL,lease_until=NULL,
             delivered_at=CASE WHEN %s IN ('delivered','available','unavailable')
                               THEN coalesce(delivered_at,now()) ELSE delivered_at END,
             updated_at=now()
           WHERE id=%s AND lease_token=%s AND lease_until>now() RETURNING id""",
            (
                status,
                str(remote_job_id) if remote_job_id is not None else None,
                error,
                retry_seconds,
                status,
                row_id,
                lease_token,
            ),
        )
    ).fetchone()
    return row is not None


async def promote(conn, paper_id: str, priority: int = 0) -> None:
    """Prioritize PDF delivery/status; do not restart completed or unavailable acquisitions."""
    await conn.execute(
        """UPDATE archive_outbox SET priority=least(priority,%s),next_attempt_at=now()
           WHERE paper_id=%s AND kind='pdf' AND status IN ('pending','error','delivered')""",
        (priority, paper_id),
    )


async def status(conn) -> dict:
    rows = await (
        await conn.execute(
            "SELECT kind,status,count(*) AS count FROM archive_outbox GROUP BY kind,status"
        )
    ).fetchall()
    summary = await (
        await conn.execute(
            """SELECT min(created_at) FILTER (WHERE status IN ('pending','error'))
               AS oldest_pending_at,
           max(delivered_at) AS last_delivered_at,
           count(*) FILTER (WHERE version IS NULL AND kind<>'metadata'
                             AND status IN ('pending','error','delivered')) AS unresolved_versions
           FROM archive_outbox"""
        )
    ).fetchone()
    coverage = await (
        await conn.execute("SELECT * FROM archive_intake_checkpoints ORDER BY category")
    ).fetchall()
    return {"counts": rows, **summary, "coverage": coverage}
