"""Exercise backoff through the real arxiv parser and pagination."""

import fcntl
import io
import threading
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import arxiv
import pytest
import requests
from requests.structures import CaseInsensitiveDict

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


class Body:
    """A response body read as urllib3's is: `read1` and no connection."""

    connection = None

    def __init__(self, content: bytes) -> None:
        self.body = io.BytesIO(content)

    def read1(self, amt: int, decode_content: bool = True) -> bytes:
        return self.body.read1(amt)

    def close(self) -> None:
        pass


@pytest.fixture
def transport(monkeypatch):
    responses, calls, sleeps = [], [], []

    def get(session, url, **kwargs):
        calls.append((url, kwargs))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, content, headers = response
        answer = requests.Response()
        answer.status_code, answer.headers = status, CaseInsensitiveDict(headers)
        answer.raw = Body(content)
        return answer

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
    monkeypatch.setattr(arxiv_source.time, "monotonic", lambda: next(times, 2380))
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


def test_an_owner_lookup_waits_on_arxiv_within_its_budget_and_nightly_keeps_its_own(transport):
    # Process, Deep read and Report as a miss wait on arXiv for LOOKUP_BUDGET at most; a
    # throttled or silent arXiv is NoAnswerError at once, not 40 minutes of backoff.
    responses, calls, sleeps = transport
    responses.extend([(429, b"", {})] * 6)
    with pytest.raises(arxiv_source.NoAnswerError):
        arxiv_source.fetch_by_ids(["2609.10001"])
    assert len(calls) == 1 and not sleeps
    connect, read = calls[0][1]["timeout"]
    assert connect <= 10 and read <= arxiv_source.LOOKUP_BUDGET
    responses.clear()
    responses.append(requests.ReadTimeout("silent"))
    with pytest.raises(arxiv_source.NoAnswerError):
        arxiv_source.fetch_by_ids(["2609.10001"])
    # The nightly intake keeps 40 minutes per page and its full backoff.
    assert arxiv_source._ArxivClient().budget == 40 * 60
    calls.clear()
    responses.clear()
    responses.extend([(429, b"", {})] * 6)
    with pytest.raises(arxiv.HTTPError):
        fetch()
    assert [s for s in sleeps if s >= 60] == [60, 120, 240, 480, 900]
    assert all(kwargs["timeout"] == (10, 60) for _, kwargs in calls)


@pytest.mark.parametrize("paced", [False, True])
def test_an_owner_lookup_ends_at_its_deadline_on_a_trickling_body(
    slow_arxiv, monkeypatch, tmp_path, paced
):
    # One deadline covers the pacing slot, the connection and the whole body (L5-3): a body that
    # trickles in keeps every socket read short, and must not keep the thread, nor the slot.
    monkeypatch.setattr(arxiv_source, "LOOKUP_BUDGET", 0.5)
    if paced:
        monkeypatch.setenv("ARXIV_PACING_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("ARXIV_PACING_DIR", raising=False)
    outcome = []

    def lookup():
        try:
            outcome.append(arxiv_source.fetch_by_ids(["2609.10001"]))
        except Exception as exc:  # noqa: BLE001 - what it raised is the assertion below
            outcome.append(exc)

    thread = threading.Thread(target=lookup, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(3)
    assert not thread.is_alive(), "the fetch outlived its deadline"
    assert time.monotonic() - started < 1.5
    assert isinstance(outcome[0], arxiv_source.NoAnswerError)
    if paced:
        with open(tmp_path / "arxiv-rate.lock", "r+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the slot is free
