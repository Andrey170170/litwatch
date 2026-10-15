"""The full-text queue's rules (docs/specs/v1/pipeline.md, sections 6 and 8): which text pass a
paper's current revision wants, and when one is queued again or no longer wanted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Literal


class PassKind(StrEnum):
    BRIEF = "brief"  # a Focus paper at central or related
    WEIGHT = "weight"  # an Area pick candidate: its Weight re-checked, with a Brief and Gist
    DEEP = "deep"  # a Deep card


class PassState(StrEnum):
    AWAITING_TEXT = "awaiting_text"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# Not started, so a pass in one of these may be dropped once its paper no longer wants it.
UNSTARTED = (PassState.AWAITING_TEXT, PassState.READY, PassState.FAILED)
LEASE = timedelta(minutes=30)
# Text passes go first in the queue (lower is sooner); the archive is asked for their text at
# its urgent priority whatever this is.
TEXT_PRIORITY = 0
# A pass awaiting text this long is listed for the owner.
AWAITING_TOO_LONG = timedelta(days=7)


@dataclass(frozen=True)
class TextPass:
    """The newest Brief or Weight re-check queued for a paper's current revision."""

    id: int
    kind: PassKind
    reason: str
    state: PassState
    arxiv_version: int | None  # of the text it read; None until read, or when unknown


@dataclass(frozen=True)
class TextRead:
    """What a pass read: the arXiv version of the text (None when the archive could not say),
    its source and the SHA-256 of it."""

    version: int | None
    source: Literal["latex", "pdf"]
    sha256: str


def wanted(focus: bool, area: bool, has_text_result: bool) -> tuple[PassKind, str] | None:
    """The text pass the paper wants, with its reason: a Brief when an active Focus topic has it
    at effective Fit central or related, else a Weight re-check when an active Area topic grades
    it must read; else, when an earlier pass gave it a Brief or full-text Weight, a Brief, so
    those follow its newest text (section 8). Both kinds make the same call."""
    if focus:
        return PassKind.BRIEF, "focus"
    if area:
        return PassKind.WEIGHT, "area"
    if has_text_result:
        return PassKind.BRIEF, "revised"
    return None


def to_queue(want: tuple[PassKind, str] | None, last: TextPass | None, version: int | None) -> bool:
    """Whether to queue the wanted pass: none was queued for this revision yet, or the last one
    is done and read an older arXiv version than the paper now has. One still to run reads the
    paper's version when it does."""
    if want is None:
        return False
    if last is None:
        return True
    if last.state != PassState.DONE or version is None:
        return False
    return last.arxiv_version is None or last.arxiv_version < version


def to_relabel(want: tuple[PassKind, str] | None, last: TextPass | None) -> bool:
    """Whether the last pass, not started, is wanted for another reason now: it is relabelled,
    not queued again (one open per revision), so it says why it runs (a paper graded must read
    in an Area before its Focus verdict)."""
    return (
        want is not None
        and last is not None
        and last.state in UNSTARTED
        and (last.kind, last.reason) != want
    )


def to_drop(want: tuple[PassKind, str] | None, last: TextPass | None) -> bool:
    """Whether the last pass is no longer wanted and has not started: it is dropped, so nothing
    is read or listed for the owner for a paper that no longer qualifies (a topic paused, the
    owner's word)."""
    return want is None and last is not None and last.state in UNSTARTED
