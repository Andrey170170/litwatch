"""The nightly run: intake, triage with the keyword audit, grading."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import intake
from litwatch.config import Config
from litwatch.jobs import watch as watch_jobs
from litwatch.jobs.context import GatewayFactory, default_gateway, model_run
from litwatch.jobs.lock import run_lock
from litwatch.store import runs

log = logging.getLogger(__name__)


async def run(
    conn: psycopg.AsyncConnection,
    config: Config,
    *,
    fetch: bool = True,
    days: int | None = None,
    limit: int | None = None,
    gateway: GatewayFactory = default_gateway,
) -> int:
    """One run under the run lock. Returns the run id.

    An incomplete harvest does not stop the rest: what arrived is triaged and graded, and
    the gap stays open for the next run. `limit` caps how many untriaged papers are taken.
    """
    until = datetime.now(UTC)
    since = until - timedelta(days=days or config.intake.window_days)
    async with (
        run_lock(conn),
        model_run(conn, config, "nightly", window=(since, until), gateway=gateway) as run,
    ):
        if fetch:
            result = await intake.collect(conn, config.intake.categories, since, run.run_id)
            await runs.finish(
                conn,
                run.run_id,
                fetched=result.fetched,
                inserted=result.inserted,
                updated=result.updated,
                intake=result.details,
            )
            run.counts["rewritten"] = len(result.rewritten)
        await watch_jobs.triage(conn, config, run, limit=limit)
        await watch_jobs.grade(conn, config, run)
        return run.run_id
