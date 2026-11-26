"""What a model call runs under, beside the work item it serves (`gates.context`): the ceiling
it reserves against (`llm.ceiling.Ceiling`: a re-run's or replay's, the job's across its
chunks) and the deadline its batched calls must be answered by (the nightly's). The Worker sets
them in its item's task (`spending`), and the Model gate reads them at each call, so no call
site passes them. They are not fields of `gates.context.Work`, as the gates cannot name the
Model gate's ceiling. A task started inside inherits them, as context variables go."""

from __future__ import annotations

import dataclasses
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from litwatch.llm.ceiling import Ceiling


@dataclass(frozen=True)
class Spending:
    """The ceiling the calls reserve against, and the deadline their batched calls must be
    answered by (aware); None where there is none: no ceiling, no batches."""

    ceiling: Ceiling | None = None
    deadline: datetime | None = None


_NOTHING = Spending()
_spending: ContextVar[Spending | None] = ContextVar("litwatch_spending", default=None)


def current() -> Spending:
    """What this task's model calls run under."""
    return _spending.get() or _NOTHING


@contextmanager
def spending(**changes: Any) -> Generator[Spending]:
    """Run the block's model calls under `changes` (`ceiling`, `deadline`), the rest as it
    stands. Not both: a batch reserves nothing against a ceiling, and no caller needs both (a
    nightly has a deadline, a ceiling-limited job a ceiling)."""
    now = dataclasses.replace(current(), **changes)
    if now.ceiling is not None and now.deadline is not None:
        raise ValueError("model calls run under a ceiling or a deadline, not both")
    token = _spending.set(now)
    try:
        yield now
    finally:
        _spending.reset(token)
