"""The shell's summary: what every page shows at its edge, read by one poll (DESIGN.md, the
shell): the status line, the sidebar's topics and the Settings sidebar's hints. Counts only; the
lists stay with the pages that show them."""

from __future__ import annotations

from fastapi import APIRouter, Request

from litwatch.api import integrations as integrations_api
from litwatch.api import schemas as s
from litwatch.api.deps import Conn, budget_lines
from litwatch.api.reading import _looks, run_status
from litwatch.feedback import calibrate
from litwatch.store import quality
from litwatch.store import taxonomy as taxonomy_store

router = APIRouter(prefix="/api")


@router.get("/summary")
async def summary(request: Request, c: Conn) -> s.Summary:
    status = await run_status(request, c)
    tree = await taxonomy_store.load(c)
    since = quality.splits_window(await quality.last_night(c))
    # What Needs you lists: last night's splits still unlabelled, and the stuck steps, passes
    # and digests the status line counts (those the owner has not acknowledged).
    splits = [v for v in await quality.splits_since(c, since) if v["label"] is None]
    stuck = (
        status.awaiting_classification
        + status.analysis_failed
        + status.ungraded
        + status.fulltext_failed
        + status.digests_failed
    )
    sessions = await calibrate.sessions(c, limit=1)
    spend = await budget_lines(c, request.app.state.config.budget)
    # Budgets are per provider: once one has a budget, the spend set against them is theirs only.
    budgeted = [line for line in spend if line.budget is not None]
    cards = await integrations_api.status(request, c)
    return s.Summary(
        status=status,
        topics=list(_looks(tree).values()),
        settings=s.SettingsHints(
            waiting=len(splits) + stuck,
            last_session=sessions[0]["drawn_at"] if sessions else None,
            misses=await quality.count_misses(c),
            topics=len(tree.topics) if tree else 0,
            classes=len(tree.classes) if tree else 0,
            spent=sum(line.spent for line in budgeted or spend),
            budget=sum(line.budget for line in budgeted) if budgeted else None,
            failing=[card.name for card in cards if card.enabled and card.failing],
        ),
    )
