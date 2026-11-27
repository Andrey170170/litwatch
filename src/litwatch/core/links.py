"""Release links (docs/specs/architecture/a2-work-gate.md, "Release links"): a link to a paper's
released code or weights with its access state, and the rules of when a find runs and when a
placeholder is checked again."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel

Kind = Literal["code", "weights"]
State = Literal["public", "gated", "placeholder", "missing"]
# What set a link's state: an automatic find, the owner's "find links", or a re-check.
FoundBy = Literal["finder", "owner", "recheck"]

# A placeholder is checked again this often, this many times, then left alone; no other state is.
RECHECK_EVERY = timedelta(days=7)
RECHECKS = 4
# An automatic find of one identity that failed is tried again, by a later trigger, until this
# many have failed.
FIND_TRIES = 3


class ReleaseLink(BaseModel):
    """One link as stored: `version` is the arXiv version its find was for, `checked_at` when
    it was last fetched, `rechecks` how many re-checks it has had."""

    id: int
    paper_id: str
    version: int | None
    url: str
    kind: Kind
    state: State
    license: str | None
    checked_at: datetime
    found_by: FoundBy
    rechecks: int = 0


def wants_find(releases_code: bool, releases_weights: bool) -> bool:
    """Whether an analysis calls for an automatic find: it says the paper releases code or
    weights."""
    return releases_code or releases_weights


@dataclass(frozen=True)
class Identity:
    """What a find of a paper is of: the paper at its arXiv `version`, or, when no version is
    known, at its last update (`updated`, as arXiv's record gives it, in UTC). Whether a find is
    of the paper as it is now is `covers`, and nothing else compares a paper's versions for
    release links: scheduling, the owner's request, the Worker before it pays, the write under
    the paper's lock, a new version stored, and what the page shows."""

    version: int | None
    updated: str | None

    @classmethod
    def of(cls, scope: Mapping[str, Any]) -> Identity:
        """The identity a find's scope names."""
        return cls(scope.get("version"), scope.get("updated"))

    @classmethod
    def at(cls, version: int | None, updated: datetime | None) -> Identity:
        """The identity of a paper stored at `version` and `updated`."""
        return cls(version, updated.astimezone(UTC).isoformat() if updated else None)

    def key(self, paper_id: str) -> str:
        """An automatic find's key: one per identity."""
        if self.version is not None:
            return f"{paper_id}@v{self.version}"
        return f"{paper_id}@{self.updated or '?'}"

    def covers(self, now: Identity) -> bool:
        """Whether a find of this identity is a find of the paper as it is `now`: of the same
        version; for a find that knew no version, of the same update, a version learnt since
        for that update included."""
        if self.version is not None:
            return self.version == now.version
        return self.updated == now.updated


@dataclass(frozen=True)
class Find:
    """One find of a paper, its work item: by the owner or the finder, of what (`of`), where it
    stands (`state`, `outcome` once ended), whether it answered ("none found" included) and with
    how many links, its error, when it ended, started or is due (`at`), and when its answer
    replaced the paper's links (`answered_at`), which orders a paper's answers."""

    id: int
    by: Literal["owner", "finder"]
    of: Identity
    state: Literal["pending", "running", "ended"]
    outcome: Literal["done", "failed"] | None
    answered: bool
    links: int | None
    error: str | None
    at: datetime
    answered_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.state != "ended"


def rechecked_again(state: State, rechecks: int) -> bool:
    """Whether a link, `rechecks` re-checks done, is checked again: a placeholder, four times."""
    return state == "placeholder" and rechecks < RECHECKS


def recheck_key(link_id: int, check: int) -> str:
    """The key of the link's `check`th re-check (1 to `RECHECKS`): each its own work item."""
    return f"{link_id}#{check}"


def next_check(checked: datetime) -> datetime:
    return checked + RECHECK_EVERY
