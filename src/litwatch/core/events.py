"""The types of the events written to the outbox (`core.Event`), as `store` writes them and
integrations read them; `litwatch.events` is their contract (docs/integrations.md)."""

PAPER_ADDED = "paper.added"
PAPER_REVISED = "paper.revised"
# Classification recorded the paper's classes for its current text, by the classifier or by hand.
PAPER_CLASSIFIED = "paper.classified"
# A Gist was written for the paper: from the abstract by analysis, from the full text later.
GIST_WRITTEN = "gist.written"
# A Brief was written for the paper from its full text, for the revision in the payload.
BRIEF_WRITTEN = "brief.written"
VERDICT_RECORDED = "verdict.recorded"
# A paper's effective grade for a Focus topic changed, by a verdict or the owner's word.
GRADE_CHANGED = "grade.changed"
PAPER_SAVED = "paper.saved"
PAPER_UNSAVED = "paper.unsaved"
# A published record was found for a paper, or the owner set one.
PUBLICATION_FOUND = "publication.found"
# The owner asked for a deep card by hand, whatever the paper's grade.
CARD_REQUESTED = "card.requested"
# A deep card was stored for a paper (emitted by the `deep` integration).
CARD_READY = "card.ready"
# A digest was published, written or "Nothing new": its id, topic, period and day.
DIGEST_WRITTEN = "digest.written"
