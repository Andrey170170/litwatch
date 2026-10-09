"""The intake job: fetch new and revised papers for the configured categories."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import intake
from litwatch.config import Config
from litwatch.store import runs

log = logging.getLogger(__name__)


async def run(
    conn: psycopg.AsyncConnection, config: Config, days: int | None = None
) -> intake.IntakeResult:
    """Harvest the window ending now and record the run. An incomplete harvest is not an
    error: the gap stays open in the checkpoints and the next run retries it."""
    until = datetime.now(UTC)
    since = until - timedelta(days=days or config.intake.window_days)
    run_id = await runs.start(conn, "intake", since, until)
    try:
        result = await intake.collect(conn, config.intake.categories, since, run_id)
    except Exception as exc:
        await runs.finish(conn, run_id, error=f"{type(exc).__name__}: {exc}")
        raise
    await runs.finish(
        conn,
        run_id,
        fetched=result.fetched,
        inserted=result.inserted,
        updated=result.updated,
        intake=result.details,
        counts={"rewritten": len(result.rewritten)},
    )
    log.info(
        "run %s intake %s: %s papers (%s new, %s updated)",
        run_id,
        result.details["status"],
        result.fetched,
        result.inserted,
        result.updated,
    )
    return result
