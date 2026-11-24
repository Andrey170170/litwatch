"""Work items (docs/specs/architecture/a2-work-gate.md, "Work items"): one attempt at one unit
of work, as the Work gate records, admits and leases it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class ItemState(StrEnum):
    PENDING = "pending"  # waits for its `not_before` and its admission
    RUNNING = "running"  # leased to a worker
    ENDED = "ended"


class Outcome(StrEnum):
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkItem:
    """A work item as stored. While it runs, `lease_token` and `lease_until` are its lease: the
    worker holding that token holds the item until then, unless renewed."""

    id: int
    kind: str
    key: str
    admission: str
    state: ItemState
    not_before: datetime
    attempts: int
    scope: dict[str, Any] = field(default_factory=dict)
    lease_token: UUID | None = None
    lease_until: datetime | None = None
    outcome: Outcome | None = None
    error: str | None = None
