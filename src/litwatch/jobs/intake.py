"""The intake jobs: fetch new and revised papers for the configured categories from arXiv,
or read a window's papers from the archive."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from litwatch import intake
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate
from litwatch.intake import archive as archive_intake
from litwatch.intake import catalog
from litwatch.intake.http import XmlClient
from litwatch.integrations import archive as archive_integration
from litwatch.jobs.context import RunRecord, run_record

log = logging.getLogger(__name__)


def _record(run: RunRecord, result: intake.IntakeResult) -> None:
    run.fields.update(
        fetched=result.fetched,
        inserted=result.inserted,
        updated=result.updated,
        intake=result.details,
    )
    run.counts["rewritten"] = len(result.rewritten)


async def run(db: DatabaseGate, config: Config, days: int | None = None) -> intake.IntakeResult:
    """Harvest the window ending now and record the run, under the run lock. An incomplete
    harvest is not an error: the gap stays open in the checkpoints and the next run retries it."""
    until = datetime.now(UTC)
    since = until - timedelta(days=days or config.intake.window_days)
    async with (
        db.held("run"),
        run_record(db, "intake", window=(since, until)) as record,
    ):
        result = await intake.collect(
            db,
            config.intake.categories,
            since,
            record.run_id,
            archive=archive_integration.harvester(config),
            rules=word.rules(config),
        )
        _record(record, result)
    log.info(
        "run %s intake %s: %s papers (%s new, %s updated, %s unchanged)",
        record.run_id,
        result.details["status"],
        result.fetched,
        result.inserted,
        result.updated,
        result.unchanged,
    )
    return result


async def catalog_from(
    db: DatabaseGate,
    config: Config,
    since: date,
    report: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run only the catalog pass (`intake.catalog`), from `since` at the latest, and record the
    run, under the run lock; its outcome is under `catalog` in the run's intake details."""
    archive = archive_integration.harvester(config)
    if archive is None:
        raise ValueError("the catalog pass is off: set harvest = true under [integrations.archive]")
    window = (datetime.combine(since, time.min, UTC), datetime.now(UTC))
    async with (
        db.held("run"),
        run_record(db, "intake", window=window) as record,
    ):
        await catalog.lower(db, since)
        with XmlClient() as client:
            done = await catalog.run(
                db, archive, config.intake.categories, since, record.run_id, client, report=report
            )
        record.fields["intake"] = {"catalog": done}
    return done


async def from_archive(
    db: DatabaseGate,
    config: Config,
    client: ArchiveClient,
    since: date,
    until: date,
) -> intake.IntakeResult:
    """Read the papers first submitted from `since` to `until` (both inclusive) in the
    configured categories from the archive, and record the run, under the run lock."""
    window = (
        datetime.combine(since, time.min, UTC),
        datetime.combine(until + timedelta(days=1), time.min, UTC),
    )
    async with (
        db.held("run"),
        run_record(db, "intake", window=window) as record,
    ):
        result = await archive_intake.collect(
            db, client, config.intake.categories, since, until, rules=word.rules(config)
        )
        _record(record, result)
    log.info(
        "run %s intake from the archive: %s papers (%s new, %s updated)",
        record.run_id,
        result.fetched,
        result.inserted,
        result.updated,
    )
    return result
