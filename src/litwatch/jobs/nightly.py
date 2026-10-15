"""The nightly run. Until classification and analysis join it (N2a), it is intake alone,
under the run lock and with a run record, as the steps that follow will need."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg

from litwatch import intake
from litwatch.config import Config
from litwatch.jobs.context import GatewayFactory, default_gateway, model_run
from litwatch.jobs.lock import run_lock
from litwatch.store import runs


async def run(
    conn: psycopg.AsyncConnection,
    config: Config,
    *,
    fetch: bool = True,
    days: int | None = None,
    gateway: GatewayFactory = default_gateway,
) -> int:
    """One run under the run lock. Returns the run id.

    An incomplete harvest is not an error: the gap stays open for the next run.
    """
    until = datetime.now(UTC)
    since = until - timedelta(days=days or config.intake.window_days)
    async with (
        run_lock(conn),
        model_run(conn, config, "nightly", window=(since, until), gateway=gateway) as run,
    ):
        if fetch:
            result = await intake.collect(conn, config.intake.categories, since, run.run_id)
            await runs.update(
                conn,
                run.run_id,
                fetched=result.fetched,
                inserted=result.inserted,
                updated=result.updated,
                intake=result.details,
            )
            run.counts["rewritten"] = len(result.rewritten)
        return run.run_id
