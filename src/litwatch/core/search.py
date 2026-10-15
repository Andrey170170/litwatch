"""The second, optional contract an integration may implement: answering searches.

The API asks every loaded `SearchProvider` beside its own search, merges the hits by arXiv
id, and leaves out a provider that fails or does not answer within a short timeout.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

# How long a search waits for each provider before leaving it out of the response. A provider
# should give up on its own well before this, since its thread cannot be stopped from outside.
SEARCH_TIMEOUT_SECONDS = 5.0


class SearchFilters(BaseModel):
    """What a provider is asked to honour. Topic and saved are litwatch's own and are not
    passed on: a paper outside the collection has neither."""

    since: date | None = None  # published on or after, by the published date
    until: date | None = None  # published on or before
    limit: int = Field(default=20, ge=1)


class SearchHit(BaseModel):
    """A paper a provider found: enough for a table row and an arXiv BibTeX entry."""

    id: str  # the version-stripped arXiv id
    title: str
    authors: list[str] = Field(default_factory=list)
    published: datetime | None = None
    score: float | None = None  # higher is better; only compared within one provider


@runtime_checkable
class SearchProvider(Protocol):
    """Synchronous on purpose: the API runs it in a thread with a timeout of its own."""

    name: str

    def search(self, query: str, filters: SearchFilters) -> list[SearchHit]: ...
