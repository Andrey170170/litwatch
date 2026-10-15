"""Domain types and pure rules. Imports nothing from the rest of litwatch and does no I/O."""

from litwatch.core.rules import (
    current_verdict,
    effective_grade,
    in_library,
    owners_word,
    rewritten,
)
from litwatch.core.search import (
    SEARCH_TIMEOUT_SECONDS,
    SearchFilters,
    SearchHit,
    SearchProvider,
)
from litwatch.core.taxonomy import Depth, Taxonomy, TaxonomyClass, Topic
from litwatch.core.types import (
    Event,
    Grade,
    Hand,
    Label,
    LabelOrigin,
    LabelValue,
    Opinion,
    Paper,
    Publication,
    Reaction,
    ReactionReason,
    ReactionValue,
    Status,
    Verdict,
    Word,
)

__all__ = [
    "SEARCH_TIMEOUT_SECONDS",
    "Depth",
    "Event",
    "Grade",
    "Hand",
    "Label",
    "LabelOrigin",
    "LabelValue",
    "Opinion",
    "Paper",
    "Publication",
    "Reaction",
    "ReactionReason",
    "ReactionValue",
    "SearchFilters",
    "SearchHit",
    "SearchProvider",
    "Status",
    "Taxonomy",
    "TaxonomyClass",
    "Topic",
    "Verdict",
    "Word",
    "current_verdict",
    "effective_grade",
    "in_library",
    "owners_word",
    "rewritten",
]
