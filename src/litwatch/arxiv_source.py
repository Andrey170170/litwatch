"""arXiv access through the `arxiv` library: lookups, searches, and the windowed daily fetch."""

import re
from collections.abc import Iterator
from datetime import datetime

import arxiv

from litwatch.models import Paper

_VERSION_RE = re.compile(r"v\d+$")


def strip_version(arxiv_id: str) -> str:
    return _VERSION_RE.sub("", arxiv_id)


def id_from_url(text: str) -> str:
    """Accepts a bare id, abs/pdf URL, or `arXiv:` prefix and returns the version-stripped id."""
    text = text.strip()
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", text)
    if not m:
        raise ValueError(f"no arXiv id in {text!r}")
    return m.group(1)


def to_paper(result: arxiv.Result) -> Paper:
    return Paper(
        id=strip_version(result.get_short_id()),
        title=" ".join(result.title.split()),
        abstract=" ".join(result.summary.split()),
        authors=[a.name for a in result.authors],
        categories=list(result.categories),
        primary_category=result.primary_category,
        published=result.published.isoformat(),
        updated=result.updated.isoformat(),
        url=result.entry_id,
    )


def fetch_by_ids(ids: list[str], client: arxiv.Client | None = None) -> list[Paper]:
    client = client or arxiv.Client()
    return [to_paper(r) for r in client.results(arxiv.Search(id_list=ids))]


def search(query: str, max_results: int, client: arxiv.Client | None = None) -> list[Paper]:
    client = client or arxiv.Client()
    s = arxiv.Search(query=query, max_results=max_results, sort_by=arxiv.SortCriterion.Relevance)
    return [to_paper(r) for r in client.results(s)]


def window_query(categories: list[str], since: datetime, until: datetime) -> str:
    """arXiv query for papers in any of `categories` submitted within [since, until]."""
    cats = " OR ".join(f"cat:{c}" for c in categories)
    fmt = "%Y%m%d%H%M"
    return f"({cats}) AND submittedDate:[{since.strftime(fmt)} TO {until.strftime(fmt)}]"


def fetch_window(
    categories: list[str],
    since: datetime,
    until: datetime,
    client: arxiv.Client | None = None,
) -> Iterator[Paper]:
    """Every paper submitted in the window, newest first. The client paces requests."""
    client = client or arxiv.Client(page_size=500, delay_seconds=3, num_retries=5)
    s = arxiv.Search(
        query=window_query(categories, since, until),
        max_results=None,
        sort_by=arxiv.SortCriterion.SubmittedDate,
    )
    for r in client.results(s):
        yield to_paper(r)
