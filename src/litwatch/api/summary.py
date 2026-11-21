"""The shell's summary: what every page shows at its edge, read by one poll (DESIGN.md, the
shell): the status line, today's date in the schedule's zone, the sidebar's topics with today's
counts and the Settings sidebar's hints. Counts only, each a narrow read in one read unit; the
lists stay with the pages that show them."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Request

from litwatch.api import integrations as integrations_api
from litwatch.api import schemas as s
from litwatch.api.deps import Db, budget_lines, protected
from litwatch.api.reading import _looks
from litwatch.core.digests import DigestPeriod
from litwatch.gates.database import ReadUnit
from litwatch.store import calibration as calibration_store
from litwatch.store import digests as digests_store
from litwatch.store import quality, reading
from litwatch.store import taxonomy as taxonomy_store

router = protected("/api")


@router.get("/summary")
async def summary(request: Request, db: Db) -> s.Summary:
    async with db.read("summary") as c:
        return await _summary(request, c)


async def _summary(request: Request, c: ReadUnit) -> s.Summary:
    config = request.app.state.config
    today = datetime.now(UTC).astimezone(config.schedule.zone).date()
    spend = await budget_lines(c, config.budget)
    status = s.RunStatus(
        **await reading.status(c), over_budget=[line.provider for line in spend if line.over]
    )
    tree = await taxonomy_store.load(c)
    counts = await digests_store.papers_written(c, DigestPeriod.DAY, today)
    since = quality.splits_window(await quality.last_night(c))
    # What Needs you waits on: last night's splits still unlabelled, and the stuck steps, passes
    # and digests the status line counts (those the owner has not acknowledged).
    stuck = (
        status.awaiting_classification
        + status.analysis_failed
        + status.ungraded
        + status.fulltext_failed
        + status.digests_failed
    )
    # Budgets are per provider: once one has a budget, the spend set against them is theirs only.
    budgeted = [line for line in spend if line.budget is not None]
    return s.Summary(
        status=status,
        today=today,
        topics=[
            s.FollowedTopic(**badge.model_dump(), count=counts.get(slug))
            for slug, badge in _looks(tree).items()
        ],
        settings=s.SettingsHints(
            waiting=await quality.count_unlabelled_splits(c, since) + stuck,
            last_session=await calibration_store.last_drawn(c),
            misses=await quality.count_misses(c),
            topics=len(tree.topics) if tree else 0,
            classes=len(tree.classes) if tree else 0,
            spent=sum(line.spent for line in budgeted or spend),
            budget=sum(line.budget for line in budgeted) if budgeted else None,
            failing=await integrations_api.failing(request, c),
        ),
    )
