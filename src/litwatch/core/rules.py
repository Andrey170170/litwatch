"""Pure rules over the domain types."""

from __future__ import annotations

import re
from collections.abc import Iterable
from difflib import SequenceMatcher

from litwatch.core.types import Grade, Label, LabelValue, Opinion, Paper, Topic, Verdict

REWRITE_SIMILARITY = 0.8  # texts less similar than this were rewritten, not touched up


def reconcile(judge: Opinion, check: Opinion) -> tuple[Grade, bool]:
    """The verdict's grade and whether it is split.

    Agreement is the grade. On disagreement the more inclusive grade stands and the verdict
    is marked split, so a person settles it.
    """
    if judge.grade == check.grade:
        return judge.grade, False
    return min(judge.grade, check.grade, key=lambda g: g.rank), True


def current_verdict(verdicts: Iterable[Verdict]) -> Verdict | None:
    """The verdict that counts for one paper and topic: version first, time second.

    Among the verdicts for the newest paper version and topic revision that have one, the
    most recently recorded. A missing paper version sorts below any known one.
    """
    return max(
        verdicts,
        key=lambda v: (v.paper_version or 0, v.topic_revision, v.recorded_at, v.id or 0),
        default=None,
    )


def effective_grade(label: Label | None, verdict: Verdict | None) -> Grade | None:
    """What every reader-facing surface shows: the owner's label, otherwise the verdict.

    Not relevant is `none`. Relevant without a grade keeps the verdict's grade when that is
    `relevant` or above, and is `relevant` otherwise. Unsure changes nothing.
    """
    graded = verdict.grade if verdict else None
    if label is None or label.value == LabelValue.UNSURE:
        return graded
    if label.value == LabelValue.NOT_RELEVANT:
        return Grade.NONE
    if label.grade is not None:
        return label.grade
    return graded if graded and graded.at_least(Grade.RELEVANT) else Grade.RELEVANT


def keyword_match(topic: Topic, paper: Paper) -> str | None:
    """The first of the topic's keyword patterns found in the title or abstract, if any."""
    text = f"{paper.title}\n{paper.abstract}"
    for pattern in topic.keywords:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern
    return None


def rewritten(title: str, abstract: str, new: Paper) -> bool:
    """A title or abstract changed beyond typo, wording and Atom-vs-canonical formatting."""

    def differs(old: str, current: str) -> bool:
        old, current = " ".join(old.split()).casefold(), " ".join(current.split()).casefold()
        return (
            old != current
            and SequenceMatcher(None, old, current, autojunk=False).ratio() < REWRITE_SIMILARITY
        )

    return differs(title, new.title) or differs(abstract, new.abstract)
