"""Release links (docs/specs/architecture/a2-work-gate.md, "Release links"): a link to a paper's
released code or weights with its access state, and the rules of when a find runs and when a
placeholder is checked again."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

Kind = Literal["code", "weights"]
State = Literal["public", "gated", "placeholder", "missing"]
# What set a link's state: an automatic find, the owner's "find links", or a re-check.
FoundBy = Literal["finder", "owner", "recheck"]

# A placeholder is checked again this often, this many times, then left alone; no other state is.
RECHECK_EVERY = timedelta(days=7)
RECHECKS = 4
# An automatic find of one version that failed is tried again, by a later trigger, until this
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


def find_key(paper_id: str, version: int | None, updated: str | None) -> str:
    """What a find is of: the paper at its arXiv `version`; at its last update (`updated`, as
    arXiv's record gives it) when no version is known, so each update is a find of its own."""
    return f"{paper_id}@v{version}" if version is not None else f"{paper_id}@{updated or '?'}"


def rechecked_again(state: State, rechecks: int) -> bool:
    """Whether a link, `rechecks` re-checks done, is checked again: a placeholder, four times."""
    return state == "placeholder" and rechecks < RECHECKS


def recheck_key(link_id: int, check: int) -> str:
    """The key of the link's `check`th re-check (1 to `RECHECKS`): each its own work item."""
    return f"{link_id}#{check}"


def next_check(checked: datetime) -> datetime:
    return checked + RECHECK_EVERY
