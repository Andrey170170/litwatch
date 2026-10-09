"""arXiv sources to stored papers: OAI-PMH harvesting, the announcement-feed fallback,
shared pacing and revision handling."""

from litwatch.intake.collect import IntakeResult, collect
from litwatch.intake.source import PaperSource

__all__ = ["IntakeResult", "PaperSource", "collect"]
