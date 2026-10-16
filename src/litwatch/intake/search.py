"""arXiv access through the `arxiv` library: lookups, searches, and the windowed daily fetch."""

import logging
import random
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import arxiv
import requests
import urllib3

from litwatch.core import Paper
from litwatch.intake.pacing import PacingError, shared_pacer

_VERSION_RE = re.compile(r"v\d+$")


log = logging.getLogger(__name__)
_BACKOFF = (60, 120, 240, 480, 900)
_RETRY_BUDGET = 40 * 60  # the nightly intake's wait on one page, retries included
# An owner's request (Process, Deep read, Report as a miss) waits this long on arXiv at most:
# the shared pacer's slot, the connection and the whole body. No backoff fits, so it is one try.
LOOKUP_BUDGET = 45.0


class NoAnswerError(RuntimeError):
    """arXiv gave no usable answer within the budget: it failed, refused or was too slow."""


def _retry_after(value: str) -> float:
    """Accept Retry-After seconds or an HTTP date; ignore malformed values."""
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


class _ArxivSession(requests.Session):
    """Bound each request by its page's deadline and retain Retry-After, which arxiv.HTTPError
    discards."""

    retry_after = 0.0

    def get(self, url, **kwargs):
        """One request within the page's deadline: the pacing slot, the connection and the whole
        body. Past it, requests.Timeout, with the connection closed and the slot free, so no
        thread is left holding them."""
        self.retry_after = 0
        deadline = getattr(self, "deadline", time.monotonic() + _RETRY_BUDGET)
        kwargs["stream"] = True
        pacer = shared_pacer()
        if pacer is None:
            response = self._read(url, deadline, None, **kwargs)
        else:
            # The API endpoint does not need redirects. Reject instead of silently
            # following one without provider pacing.
            kwargs["allow_redirects"] = False
            with pacer.slot(timeout=max(0, deadline - time.monotonic())):
                response = self._read(url, deadline, pacer, **kwargs)
        self.retry_after = _retry_after(response.headers.get("Retry-After", ""))
        if response.status_code == 200:
            try:
                root = ET.fromstring(response.content)
                total = root.find("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
                if (
                    root.tag != "{http://www.w3.org/2005/Atom}feed"
                    or total is None
                    or int(total.text or "") < 0
                ):
                    raise ValueError("not an arXiv Atom result feed")
            except (ET.ParseError, ValueError) as exc:
                raise _IncompleteFeedError("arXiv returned an invalid result feed") from exc
        return response

    def _read(self, url, deadline: float, pacer, **kwargs) -> requests.Response:
        """The response with its body read, chunk by chunk against `deadline`."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout("arXiv's deadline passed")
        kwargs.setdefault("timeout", (min(10, remaining), min(60, remaining)))
        response = super().get(url, **kwargs)
        try:
            if pacer is not None:
                pacer.defer(response.headers.get("Retry-After"))
            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise requests.ReadTimeout("arXiv's deadline passed mid-body")
                # Each read is one socket read, and waits no longer than the deadline allows.
                connection = response.raw.connection
                if connection is not None and connection.sock is not None:
                    connection.sock.settimeout(min(60, remaining))
                try:
                    chunk = response.raw.read1(65536, decode_content=True)
                except urllib3.exceptions.ReadTimeoutError as exc:
                    raise requests.ReadTimeout(exc) from exc
                except urllib3.exceptions.ProtocolError as exc:
                    raise requests.exceptions.ChunkedEncodingError(exc) from exc
                except urllib3.exceptions.DecodeError as exc:
                    raise requests.exceptions.ContentDecodingError(exc) from exc
                if not chunk:
                    break
                body += chunk
            response._content = bytes(body)
        finally:
            response.close()
        return response


class _IncompleteFeedError(ValueError):
    """A malformed response must not count as a successful empty intake."""


class _ArxivClient(arxiv.Client):
    """Retry one page at a time; successful pages are never replayed."""

    def __init__(self, page_size: int = 100, *, budget: float = _RETRY_BUDGET):
        # One retry owner: disable the library's rapid recursive retry loop.
        super().__init__(page_size=page_size, delay_seconds=3, num_retries=0)
        self._session.close()
        self._session = _ArxivSession()
        self.budget = budget  # seconds per page, retries included

    def _parse_feed(self, url: str, first_page: bool = True, _try_index: int = 0):
        # arxiv has no public transport/parser seam; real-parser tests cover this override.
        deadline = time.monotonic() + self.budget
        self._session.deadline = deadline
        for attempt in range(len(_BACKOFF) + 1):
            try:
                feed = super()._parse_feed(url, first_page, _try_index=0)
                if feed.malformed or (not feed.results and feed.header.total_results > 0):
                    raise _IncompleteFeedError("arXiv returned a malformed feed")
                return feed
            except (
                arxiv.HTTPError,
                arxiv.UnexpectedEmptyPageError,
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
                _IncompleteFeedError,
            ) as exc:
                if isinstance(exc, requests.exceptions.SSLError):
                    raise
                if isinstance(exc, arxiv.HTTPError) and exc.status not in (429, 500, 502, 503, 504):
                    raise
                if attempt == len(_BACKOFF):
                    raise
                delay = min(900, _BACKOFF[attempt] * random.uniform(1, 1.1))
                delay = max(delay, self._session.retry_after)
                # Never shorten the server's cooldown to fit our budget.
                if time.monotonic() + delay + 70 > deadline:
                    raise
                log.warning(
                    "arXiv page failed (%s); retry %s/%s in %.0fs",
                    type(exc).__name__
                    + (f" HTTP {exc.status}" if isinstance(exc, arxiv.HTTPError) else ""),
                    attempt + 1,
                    len(_BACKOFF),
                    delay,
                )
                time.sleep(delay)
        raise AssertionError("unreachable")


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
    """The papers arXiv has of `ids`, for an owner's request: within `LOOKUP_BUDGET`, else
    NoAnswerError."""
    client = client or _ArxivClient(budget=LOOKUP_BUDGET)
    try:
        return [to_paper(r) for r in client.results(arxiv.Search(id_list=ids))]
    except (requests.RequestException, arxiv.ArxivError, _IncompleteFeedError, PacingError) as exc:
        raise NoAnswerError(f"{type(exc).__name__}: {exc}"[:300]) from exc


def search(query: str, max_results: int, client: arxiv.Client | None = None) -> list[Paper]:
    client = client or _ArxivClient()
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
    client = client or _ArxivClient(page_size=500)
    s = arxiv.Search(
        query=window_query(categories, since, until),
        max_results=None,
        sort_by=arxiv.SortCriterion.SubmittedDate,
    )
    for r in client.results(s):
        yield to_paper(r)
