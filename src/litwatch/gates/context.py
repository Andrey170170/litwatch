"""What a gate call serves, carried in context variables (docs/specs/architecture/a2-work-gate.md,
"Leases" and "Attribution").

- The work context: the work item a Worker runs, with its lease. The Work gate sets it in the
  item's task; the Database gate fences every write unit opened under it on that lease
  (`gates.database`), and the Model gate records its id on each call. What the item may spend
  and when its batched calls must be answered by are the Model gate's (`llm.context`): the
  gates cannot name its ceiling.
- The attribution: a work item, a request, or a named system attribution. The Work gate sets
  it for an item's task and for its own bookkeeping; Entries set theirs in P5, which also turns
  on the check (`attribution()` read by the gates).

A task started inside either inherits it, as context variables go. A gate's own bookkeeping
for many items (`bookkeeping`) runs outside both."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from litwatch.core.work import WorkItem


@dataclass(frozen=True)
class Work:
    """The work item a task runs under its lease (`item.lease_token`)."""

    item: WorkItem


@dataclass(frozen=True)
class Attribution:
    """Whom a gate call serves: a work item (`work`, named by its id), a request (`request`,
    named by its route or command) or the system (`system`, named by what runs: startup, a
    gate's bookkeeping)."""

    kind: Literal["work", "request", "system"]
    name: str

    @classmethod
    def work(cls, item_id: int) -> Attribution:
        return cls("work", str(item_id))

    @classmethod
    def system(cls, name: str) -> Attribution:
        return cls("system", name)


_work: ContextVar[Work | None] = ContextVar("litwatch_work", default=None)
_attribution: ContextVar[Attribution | None] = ContextVar("litwatch_attribution", default=None)


def current_work() -> Work | None:
    """The work item this task runs, if any."""
    return _work.get()


def attribution() -> Attribution | None:
    """Whom this task's gate calls serve, if it was set."""
    return _attribution.get()


@contextmanager
def working(work: Work) -> Generator[None]:
    """Run the block under `work`: its write units are fenced on the item's lease."""
    token = _work.set(work)
    try:
        yield
    finally:
        _work.reset(token)


@contextmanager
def attributed(who: Attribution) -> Generator[None]:
    """Attribute the block's gate calls to `who`."""
    token = _attribution.set(who)
    try:
        yield
    finally:
        _attribution.reset(token)


@contextmanager
def bookkeeping(name: str) -> Generator[None]:
    """Run the block as a gate's own bookkeeping, under the system attribution `name` and
    outside any work item, so its write units are not fenced: what it records (a model call's
    charge, a batch's answers) serves items that may be gone, and stands whether or not the
    item's lease does."""
    work, who = _work.set(None), _attribution.set(Attribution.system(name))
    try:
        yield
    finally:
        _attribution.reset(who)
        _work.reset(work)
