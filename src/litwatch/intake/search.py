"""arXiv ids, and the owner's lookups on arXiv, through the `arxiv` library's query format and
feed parser. The nightly intake reads OAI-PMH (`intake.oai`)."""

import asyncio
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import arxiv
import arxiv._feed
import httpx

from litwatch.core import Paper
from litwatch.intake.pacing import PacingError, async_response

_VERSION_RE = re.compile(r"v\d+$")

# An owner's request (Process, Deep read, Report as a miss) waits this long on arXiv at most:
# the shared pacer's slot, the connection, the headers, the body and the parse. No backoff
# fits, so it is one try.
LOOKUP_BUDGET = 45.0
# The most of arXiv's answer a lookup reads: one paper's feed is a few kilobytes.
_LOOKUP_BYTES = 4 * 1024 * 1024


class NoAnswerError(RuntimeError):
    """arXiv gave no usable answer within the budget: it failed, refused or was too slow."""


def _check_feed(content: bytes) -> None:
    """Raise _IncompleteFeedError unless `content` is an arXiv Atom result feed."""
    try:
        root = ET.fromstring(content)
        total = root.find("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
        if (
            root.tag != "{http://www.w3.org/2005/Atom}feed"
            or total is None
            or int(total.text or "") < 0
        ):
            raise ValueError("not an arXiv Atom result feed")
    except (ET.ParseError, ValueError, LookupError) as exc:  # LookupError: an unknown encoding
        raise _IncompleteFeedError("arXiv returned an invalid result feed") from exc


class _IncompleteFeedError(ValueError):
    """A malformed answer must not read as arXiv lacking the paper."""


def strip_version(arxiv_id: str) -> str:
    return _VERSION_RE.sub("", arxiv_id)


def id_from_url(text: str) -> str:
    """Accepts a bare id, abs/pdf URL, or `arXiv:` prefix and returns the version-stripped id.
    Old-style ids (`cs/9901001`, `math.AG/0601001`) are accepted too."""
    text = text.strip()
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", text) or re.search(
        r"\b([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?", text
    )
    if not m:
        raise ValueError(f"no arXiv id in {text!r}")
    return m.group(1)


def to_paper(result: arxiv.Result) -> Paper:
    return Paper(
        id=strip_version(result.get_short_id()),
        version=(
            int(m.group(1)) if (m := re.search(r"v([1-9]\d*)$", result.get_short_id())) else None
        ),
        doi=getattr(result, "doi", None),
        journal_ref=getattr(result, "journal_ref", None),
        comments=getattr(result, "comment", None),
        title=" ".join(result.title.split()),
        abstract=" ".join(result.summary.split()),
        authors=[a.name for a in result.authors],
        categories=list(result.categories),
        primary_category=result.primary_category,
        published=result.published.isoformat(),
        updated=result.updated.isoformat(),
        url=result.entry_id,
    )


async def fetch_by_ids(ids: list[str], client: httpx.AsyncClient | None = None) -> list[Paper]:
    """The papers arXiv has of `ids`, for an owner's request: one try, within `LOOKUP_BUDGET`
    from the shared pacing slot through the connection, the headers, the body and the parse;
    else NoAnswerError. At the deadline the request is cancelled: its connection closes, then
    the slot is released. The nightly intake keeps its own client, budget and backoff."""
    args = {"search_query": "", "id_list": ",".join(ids), "start": 0, "max_results": len(ids)}
    url = arxiv.Client.query_url_format.format(urlencode(args))
    try:
        async with asyncio.timeout(LOOKUP_BUDGET):
            async with httpx.AsyncClient(timeout=LOOKUP_BUDGET) as own:
                agent = {"user-agent": arxiv._USER_AGENT}
                async with async_response(client or own, "GET", url, headers=agent) as response:
                    if response.status_code != 200:
                        raise NoAnswerError(f"arXiv answered HTTP {response.status_code}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > _LOOKUP_BYTES:
                            raise NoAnswerError("arXiv's answer is larger than a lookup reads")
            return await asyncio.to_thread(_papers, bytes(body))
    except TimeoutError as exc:
        raise NoAnswerError("arXiv gave no answer in time") from exc
    except (httpx.HTTPError, PacingError, _IncompleteFeedError) as exc:
        raise NoAnswerError(f"{type(exc).__name__}: {exc}"[:300]) from exc


def _papers(content: bytes) -> list[Paper]:
    """The papers of an arXiv result feed, read by the `arxiv` library's parser, checked as the
    nightly intake checks a page: a total with no entry read is a feed cut short, and an entry
    the parser or `Paper` cannot read is no answer, not "arXiv lacks it"."""
    _check_feed(content)
    try:
        feed = arxiv._feed.parse(content)
        if feed.malformed or (not feed.results and feed.header.total_results > 0):
            raise _IncompleteFeedError("arXiv returned a malformed feed")
        return [to_paper(r) for r in feed.results]
    except _IncompleteFeedError:
        raise
    # A timestamp fromisoformat refuses or cannot carry to UTC, a Paper that fails validation.
    except (ValueError, OverflowError) as exc:
        raise _IncompleteFeedError(f"arXiv returned an entry litwatch cannot read: {exc}") from exc
