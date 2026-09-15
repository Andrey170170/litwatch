"""Exercise backoff through the real arxiv parser and pagination."""

from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import arxiv
import pytest
import requests

from litwatch import arxiv_source


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
