"""Pure rules over the domain types."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from difflib import SequenceMatcher

from litwatch.core.grading import Fit
from litwatch.core.types import (
    Grade,
    Hand,
    Label,
    LabelValue,
    Paper,
    Reaction,
    ReactionReason,
)

REWRITE_SIMILARITY = 0.8  # texts less similar than this were rewritten, not touched up
# Runs in a row a step may fail on a paper's text before the owner is shown it (pipeline.md,
# section 9): classification then waits for the owner, analysis is listed on Quality.
FAILED_RUNS = 3
# The two reasons that make a reaction the owner's word on a Focus topic.
REACTION_WORDS = (ReactionReason.UNRELATED, ReactionReason.GREAT_FIND)


_NOW = datetime.max.replace(tzinfo=UTC)


def owners_word(
    label: Label | None, reaction: Reaction | None, verdict: Fit | None, *, focus: bool
) -> Fit | None:
    """The owner's word on a paper for a topic, a Fit (feedback.md): the newer of the label and
    a word-carrying reaction. A reported miss is a label. An unsure label is no word, and a
    provisional label, accepted in bulk, is older than any reaction. A reaction speaks on a
    Focus topic only: wrong topic is none; great find is the verdict's Fit if central or
    related, else related. A time not yet stored counts as now."""
    if label is not None and label.value == LabelValue.UNSURE:
        label = None
    if not focus or (reaction is not None and reaction.reason not in REACTION_WORDS):
        reaction = None
    labelled = Fit(label.value) if label else None
    if reaction is None:
        return labelled
    if reaction.reason == ReactionReason.UNRELATED:
        heard = Fit.NONE
    else:
        heard = verdict if verdict in (Fit.CENTRAL, Fit.RELATED) else Fit.RELATED
    if label is None or label.provisional:
        return heard
    if (label.labelled_at or _NOW) > (reaction.reacted_at or _NOW):
        return labelled
    return heard


def in_library(hand: Hand | None, grades: Iterable[Grade | None]) -> bool:
    """Whether a paper is saved: the owner's newest use of the toggle, else any Focus topic at
    an effective grade of `relevant` or above."""
    if hand is not None:
        return hand == Hand.SAVE
    return any(g is not None and g.at_least(Grade.RELEVANT) for g in grades)


def rewritten(title: str, abstract: str, new: Paper) -> bool:
    """A title or abstract changed beyond typo, wording and Atom-vs-canonical formatting."""

    def differs(old: str, current: str) -> bool:
        old, current = " ".join(old.split()).casefold(), " ".join(current.split()).casefold()
        return (
            old != current
            and SequenceMatcher(None, old, current, autojunk=False).ratio() < REWRITE_SIMILARITY
        )

    return differs(title, new.title) or differs(abstract, new.abstract)
