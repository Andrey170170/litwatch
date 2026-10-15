"""Pure rules over the domain types."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from difflib import SequenceMatcher

from litwatch.core.types import (
    Grade,
    Hand,
    Label,
    LabelValue,
    Paper,
    Reaction,
    ReactionReason,
    Verdict,
    Word,
)

REWRITE_SIMILARITY = 0.8  # texts less similar than this were rewritten, not touched up


def current_verdict(verdicts: Iterable[Verdict]) -> Verdict | None:
    """The verdict that counts for one paper and topic: text first, time second.

    Among the verdicts for the newest paper revision and topic revision that have one, the
    most recently recorded. The arXiv version plays no part: it is often unknown, and a new
    version with the same text is the same paper to a grader.
    """
    return max(
        verdicts,
        key=lambda v: (v.paper_revision, v.topic_revision, v.recorded_at, v.id or 0),
        default=None,
    )


# The two reasons that make a reaction the owner's word on a Focus topic.
WORDS = {
    ReactionReason.UNRELATED: LabelValue.NOT_RELEVANT,
    ReactionReason.GREAT_FIND: LabelValue.RELEVANT,
}
_NOW = datetime.max.replace(tzinfo=UTC)


def owners_word(label: Label | None, reaction: Reaction | None) -> Word | None:
    """The owner's word on a paper for a Focus topic: the newer of the label and the reaction.

    Only two reasons make a reaction a word ("unrelated" and "great find"). An unsure label is
    no word, and a provisional label, accepted in bulk, is older than either. A time not yet
    stored counts as now.
    """
    if label is not None and label.value == LabelValue.UNSURE:
        label = None
    if reaction is not None and reaction.reason not in WORDS:
        reaction = None
    if reaction is None:
        return Word(value=label.value, grade=label.grade) if label else None
    heard = Word(value=WORDS[reaction.reason])
    if label is None or label.provisional:
        return heard
    if (label.labelled_at or _NOW) > (reaction.reacted_at or _NOW):
        return Word(value=label.value, grade=label.grade)
    return heard


def effective_grade(word: Word | Label | None, verdict: Verdict | None) -> Grade | None:
    """What every reader-facing surface shows: the owner's word, otherwise the verdict.

    Not relevant is `none` and adjacent is `adjacent`. Relevant without a grade keeps the
    verdict's grade when that is `relevant` or above, and is `relevant` otherwise. Unsure
    changes nothing.
    """
    graded = verdict.grade if verdict else None
    if word is None or word.value == LabelValue.UNSURE:
        return graded
    if word.value == LabelValue.NOT_RELEVANT:
        return Grade.NONE
    if word.value == LabelValue.ADJACENT:
        return Grade.ADJACENT
    if word.grade is not None:
        return word.grade
    return graded if graded and graded.at_least(Grade.RELEVANT) else Grade.RELEVANT


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
