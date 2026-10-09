"""Domain types and pure rules. Imports nothing from the rest of litwatch and does no I/O."""

from litwatch.core.rules import (
    current_verdict,
    effective_grade,
    keyword_match,
    reconcile,
    rewritten,
)
from litwatch.core.types import (
    Candidate,
    CandidateState,
    Event,
    Grade,
    Label,
    LabelOrigin,
    LabelValue,
    Membership,
    Opinion,
    Paper,
    Status,
    Topic,
    TopicKind,
    Verdict,
)

__all__ = [
    "Candidate",
    "CandidateState",
    "Event",
    "Grade",
    "Label",
    "LabelOrigin",
    "LabelValue",
    "Membership",
    "Opinion",
    "Paper",
    "Status",
    "Topic",
    "TopicKind",
    "Verdict",
    "current_verdict",
    "effective_grade",
    "keyword_match",
    "reconcile",
    "rewritten",
]
