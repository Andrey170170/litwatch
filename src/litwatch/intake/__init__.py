"""Sources to stored papers: OAI-PMH harvesting, the announcement-feed fallback, shared
pacing and revision handling; and a date window read from the archive (`intake.archive`)."""

from litwatch.intake.collect import IntakeResult, collect
from litwatch.intake.source import PaperSource

__all__ = ["IntakeResult", "PaperSource", "collect"]
