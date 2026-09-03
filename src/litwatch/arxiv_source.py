"""arXiv access through the `arxiv` library. Phase 1 needs lookups and searches only."""

import re

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
        published=result.published.date().isoformat(),
        url=result.entry_id,
    )


def fetch_by_ids(ids: list[str], client: arxiv.Client | None = None) -> list[Paper]:
    client = client or arxiv.Client()
    return [to_paper(r) for r in client.results(arxiv.Search(id_list=ids))]


def search(query: str, max_results: int, client: arxiv.Client | None = None) -> list[Paper]:
    client = client or arxiv.Client()
    s = arxiv.Search(query=query, max_results=max_results, sort_by=arxiv.SortCriterion.Relevance)
    return [to_paper(r) for r in client.results(s)]
