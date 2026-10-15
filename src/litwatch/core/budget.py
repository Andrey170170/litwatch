"""Spend against budget (docs/specs/v1/models.md, budgets; decision 50): month-to-date spend per
provider account, projected to the month's end. Over budget turns the status line amber;
nothing is downgraded or stopped."""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

from pydantic import BaseModel

# A projection from the first hours of a month says little; spend is spread over at least a day.
MIN_ELAPSED = timedelta(days=1)


class Spend(BaseModel):
    """One provider's month: spent so far, projected to the end, and its budget (None: none)."""

    provider: str
    spent: float
    projected: float
    budget: float | None = None
    over: bool = False  # projected past the budget


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def projected(spent: float, now: datetime) -> float:
    """Month-to-date spend carried on at the month's rate so far to the month's end."""
    days = calendar.monthrange(now.year, now.month)[1]
    elapsed = max(now - month_start(now), MIN_ELAPSED)
    return spent * (timedelta(days=days) / elapsed)


def spend(spent: dict[str, float], budgets: dict[str, float], now: datetime) -> list[Spend]:
    """A line per provider that spent this month or has a budget, by provider name."""
    lines = []
    for p in sorted(set(spent) | set(budgets)):
        ahead = projected(spent.get(p, 0.0), now)
        budget = budgets.get(p)
        lines.append(
            Spend(
                provider=p,
                spent=round(spent.get(p, 0.0), 4),
                projected=round(ahead, 4),
                budget=budget,
                over=budget is not None and ahead > budget,
            )
        )
    return lines
