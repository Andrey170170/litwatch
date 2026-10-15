"""The full-text queue's rules (docs/specs/v1/pipeline.md, sections 6 and 8): which text pass a
paper's current revision wants, and when one is queued again or no longer wanted; and the same
for a paper's Deep card pass."""

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
# Deep passes are worked by their own worker, one at a time: the owner's requests first.
REQUEST_PRIORITY = 0
AUTOMATIC_PRIORITY = 1
# Why a Deep pass runs: the owner asked (never dropped by a settle), or a Focus topic grades the
# paper must read. N4 adds Area picks.
REQUESTED = "request"
FOCUS = "focus"
# A pass awaiting text this long is listed for the owner.
AWAITING_TOO_LONG = timedelta(days=7)
# The nightly reconcile also looks at verdicts and labels this recent, whose settle may have
# been missed: more than a night, so a missed trigger costs at most one.
RECONCILE_LOOKBACK = timedelta(days=2)


@dataclass(frozen=True)
class TextPass:
    """The newest Brief or Weight re-check queued for a paper's current revision."""

    id: int
    kind: PassKind
    reason: str
    state: PassState
    arxiv_version: int | None  # of the text it read; None until read, or when unknown
    # Done, and the paper's arXiv record was updated after the update it read the text for.
    newer_update: bool = False


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
    is done and read an older arXiv version than the paper now has, or, the paper's version
    unknown (its history missing), was read before the paper's latest update. One still to
    run reads the paper's version when it does."""
    if want is None:
        return False
    if last is None:
        return True
    if last.state != PassState.DONE:
        return False
    if version is None:
        return last.newer_update
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


@dataclass(frozen=True)
class DeepPass:
    """The paper's open Deep pass: one not done."""

    id: int
    reason: str
    state: PassState


def deep_wanted(focus_must_read: bool, saved: bool, has_card: bool) -> str | None:
    """Why the paper wants an automatic Deep card, or None: an active Focus topic grades it must
    read, it is not unsaved, and it has no card yet. A card is remade only on request
    (section 8), and an Unsave cancels automatic cards not started (integrations.md)."""
    return FOCUS if focus_must_read and saved and not has_card else None


def deep_to_queue(want: str | None, open_pass: DeepPass | None) -> bool:
    return want is not None and open_pass is None


def deep_to_drop(want: str | None, open_pass: DeepPass | None) -> bool:
    """Whether an automatic Deep pass not started is no longer wanted. The owner's request is
    never dropped this way."""
    return (
        want is None
        and open_pass is not None
        and open_pass.reason != REQUESTED
        and open_pass.state in UNSTARTED
    )
