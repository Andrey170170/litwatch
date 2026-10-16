"""Exercise backoff through the real arxiv parser and pagination."""

import asyncio
import fcntl
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import arxiv
import httpx
import pytest
import requests

from litwatch.intake import search as arxiv_source


def feed(paper_id="2609.10001", total=1):
    return f"""<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
      <opensearch:totalResults>{total}</opensearch:totalResults>
      <entry><id>http://arxiv.org/abs/{paper_id}v1</id><title>Test</title>
      <summary>Abstract</summary><author><name>Author</name></author>
      <published>2026-09-10T12:00:00Z</published><updated>2026-09-10T12:00:00Z</updated>
      <arxiv:primary_category term="cs.CL"/><category term="cs.CL"/>
      </entry></feed>""".encode()


@pytest.fixture
def transport(monkeypatch):
    responses, calls, sleeps = [], [], []

    def get(session, url, **kwargs):
        calls.append((url, kwargs))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, content, headers = response
        return SimpleNamespace(status_code=status, content=content, headers=headers)

    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr("random.uniform", lambda a, b: 1)
    return responses, calls, sleeps


def fetch():
    return list(
        arxiv_source.fetch_window(
            ["cs.CL"], datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 13, tzinfo=UTC)
        )
    )


def test_rate_limit_recovers_without_restarting_completed_pages(transport):
    responses, calls, sleeps = transport
    responses.extend(
        [(200, feed(total=2), {}), (429, b"", {}), (200, feed("2609.10002", total=2), {})]
    )
    assert [p.id for p in fetch()] == ["2609.10001", "2609.10002"]
    assert [parse_qs(urlparse(url).query)["start"] for url, _ in calls] == [["0"], ["1"], ["1"]]
    assert 60 in sleeps
    assert all(kwargs["timeout"] == (10, 60) for _, kwargs in calls)


def test_persistent_throttling_has_bounded_backoff(transport):
    responses, calls, sleeps = transport
    responses.extend([(429, b"", {})] * 6)
    with pytest.raises(arxiv.HTTPError):
        fetch()
    assert len(calls) == 6
    assert [s for s in sleeps if s >= 60] == [60, 120, 240, 480, 900]


def test_permanent_client_error_is_not_retried(transport):
    responses, calls, sleeps = transport
    responses.append((400, b"", {}))
    with pytest.raises(arxiv.HTTPError):
        fetch()
    assert len(calls) == 1
    assert not sleeps


@pytest.mark.parametrize("failure", [(503, b"", {}), requests.Timeout("upstream timeout")])
def test_transient_server_and_transport_errors_recover(transport, failure):
    responses, calls, sleeps = transport
    responses.extend([failure, (200, feed(), {})])
    assert len(fetch()) == 1
    assert 60 in sleeps


def test_retry_after_is_respected(transport):
    responses, calls, sleeps = transport
    responses.extend([(429, b"", {"Retry-After": "180"}), (200, feed(), {})])
    assert len(fetch()) == 1
    assert 180 in sleeps


def test_long_retry_after_fails_without_retrying_early(transport):
    responses, calls, sleeps = transport
    responses.append((429, b"", {"Retry-After": "7200"}))
    with pytest.raises(arxiv.HTTPError):
        fetch()
    assert len(calls) == 1
    assert not sleeps


def test_unexpected_empty_second_page_recovers(transport):
    responses, calls, sleeps = transport
    empty = feed(total=2).split(b"<entry>")[0] + b"</feed>"
    responses.extend(
        [(200, feed(total=2), {}), (200, empty, {}), (200, feed("2609.10002", total=2), {})]
    )
    assert [p.id for p in fetch()] == ["2609.10001", "2609.10002"]
    assert 60 in sleeps


def test_malformed_response_recovers(transport):
    responses, calls, sleeps = transport
    responses.extend([(200, b"<html>upstream error</html>", {}), (200, feed(), {})])
    assert len(fetch()) == 1
    assert 60 in sleeps


def test_valid_empty_feed_needs_no_retry(transport):
    responses, calls, sleeps = transport
    empty = feed(total=0).split(b"<entry>")[0] + b"</feed>"
    responses.append((200, empty, {}))
    assert fetch() == []
    assert len(calls) == 1


def test_certificate_error_is_not_retried(transport):
    responses, calls, sleeps = transport
    responses.append(requests.exceptions.SSLError("invalid certificate"))
    with pytest.raises(requests.exceptions.SSLError):
        fetch()
    assert len(calls) == 1
    assert not sleeps


@pytest.mark.parametrize("header", ["garbage", "", "-1"])
def test_invalid_retry_after_uses_backoff(transport, header):
    responses, calls, sleeps = transport
    responses.extend([(429, b"", {"Retry-After": header}), (200, feed(), {})])
    assert len(fetch()) == 1
    assert 60 in sleeps


def test_retry_after_http_date(transport):
    from datetime import timedelta
    from email.utils import format_datetime

    responses, calls, sleeps = transport
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=180), usegmt=True)
    responses.extend([(429, b"", {"Retry-After": date}), (200, feed(), {})])
    assert len(fetch()) == 1
    assert any(178 <= s <= 180 for s in sleeps)


def test_budget_counts_request_time(transport, monkeypatch):
    responses, calls, sleeps = transport
    responses.append((429, b"", {}))
    times = iter([0, 2380])
    monkeypatch.setattr(arxiv_source.time, "monotonic", lambda: next(times))
    with pytest.raises(arxiv.HTTPError):
        fetch()
    assert len(calls) == 1
    assert not sleeps


def test_interrupted_response_recovers(transport):
    responses, calls, sleeps = transport
    responses.extend(
        [requests.exceptions.ChunkedEncodingError("truncated body"), (200, feed(), {})]
    )
    assert len(fetch()) == 1
    assert 60 in sleeps


# -- an owner's lookup (Process, Deep read, Report as a miss) ---------------------------------


def arxiv_answering(*answers):
    """An httpx client whose arXiv answers each request with the next of `answers`."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status, content = answers[len(calls) - 1]
        return httpx.Response(status, content=content)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), calls


async def test_an_owner_lookup_reads_arxivs_feed_in_one_try(monkeypatch):
    monkeypatch.delenv("ARXIV_PACING_DIR", raising=False)
    client, calls = arxiv_answering((200, feed()))
    (found,) = await arxiv_source.fetch_by_ids(["2609.10001"], client)
    assert (found.id, found.version, found.title) == ("2609.10001", 1, "Test")
    assert parse_qs(calls[0].url.query.decode())["id_list"] == ["2609.10001"]
    # A throttled arXiv, a page that is no feed, an answer too large: no answer, at once.
    for answer in ((429, b""), (200, b"<html/>"), (200, b" " * (4 * 1024 * 1024 + 1))):
        client, calls = arxiv_answering(answer)
        with pytest.raises(arxiv_source.NoAnswerError):
            await arxiv_source.fetch_by_ids(["2609.10001"], client)
        assert len(calls) == 1
    # An entry the parser skips (no published) leaves a total with no entries: not "arXiv lacks
    # it". One it cannot read (a bad timestamp, one out of range in UTC) or a feed in an unknown
    # encoding is no answer either, not an error of litwatch's.
    published = "<published>2026-09-10T12:00:00Z</published>"
    for broken in (
        feed().replace(published.encode(), b""),
        feed().replace(published.encode(), b"<published>soon</published>"),
        feed().replace(published.encode(), b"<published>0001-01-01T00:00:00+23:00</published>"),
        b'<?xml version="1.0" encoding="unknown-charset"?>' + feed(),
    ):
        client, _ = arxiv_answering((200, broken))
        with pytest.raises(arxiv_source.NoAnswerError):
            await arxiv_source.fetch_by_ids(["2609.10001"], client)


@pytest.mark.parametrize("paced", [False, True])
@pytest.mark.parametrize("trickle", ["headers", "body"])
async def test_an_owner_lookup_ends_at_its_deadline_however_arxiv_trickles(
    slow_arxiv, monkeypatch, tmp_path, trickle, paced
):
    # One deadline covers the pacing slot, the connection, the headers, the body and the parse
    # (L6-4): bytes trickling in keep every socket read short, and must not keep the request,
    # nor the slot.
    slow_arxiv.trickle = trickle
    monkeypatch.setattr(arxiv_source, "LOOKUP_BUDGET", 0.5)
    if paced:
        monkeypatch.setenv("ARXIV_PACING_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("ARXIV_PACING_DIR", raising=False)
    started = time.monotonic()
    with pytest.raises(arxiv_source.NoAnswerError):
        await asyncio.wait_for(arxiv_source.fetch_by_ids(["2609.10001"]), 3)
    assert time.monotonic() - started < 0.5 + 0.3
    if paced:
        with open(tmp_path / "arxiv-rate.lock", "r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the slot is free
