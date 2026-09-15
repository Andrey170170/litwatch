from io import BytesIO

import pytest
import requests

from litwatch.source_http import SourceError, XmlClient


class Response:
    def __init__(self, status=200, body=b"<feed/>", headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}
        self.raw = self
        self._buffer = BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read1(self, size, decode_content=False):
        return self._buffer.read1(size)


@pytest.fixture
def setup(monkeypatch):
    now = [100.0]
    sleeps = []
    monkeypatch.setattr("litwatch.source_http.time.monotonic", lambda: now[0])

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr("litwatch.source_http.time.sleep", sleep)
    client = XmlClient()
    calls = []

    def responses(items):
        sequence = iter(items)

        def get(*args, **kwargs):
            calls.append(kwargs)
            item = next(sequence)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(client._session, "get", get)

    return client, responses, sleeps, calls


def test_shared_pacing_and_timeout(setup):
    client, responses, sleeps, calls = setup
    responses([Response(), Response()])
    assert client.get("https://example.org") == b"<feed/>"
    client.get("https://example.org")
    assert sleeps == [3]
    assert calls[0]["timeout"] == (10, 30)
    assert calls[0]["stream"] is True


def test_retry_after_and_malformed_xml(setup):
    client, responses, sleeps, _ = setup
    responses([Response(429, headers={"Retry-After": "40"}), Response(body=b"broken"), Response()])
    assert client.get("https://example.org") == b"<feed/>"
    assert 40 in sleeps and 30 in sleeps


@pytest.mark.parametrize("response", [Response(404), requests.exceptions.SSLError("invalid")])
def test_permanent_errors_not_retried(setup, response):
    client, responses, _, calls = setup
    responses([response])
    with pytest.raises(SourceError):
        client.get("https://example.org")
    assert len(calls) == 1


def test_retry_budget_never_shortens_server_delay(setup):
    client, responses, sleeps, calls = setup
    responses([Response(503, headers={"Retry-After": "3600"})])
    with pytest.raises(SourceError, match="budget"):
        client.get("https://example.org")
    assert sleeps == [] and len(calls) == 1


def test_finite_network_attempts(setup):
    client, responses, _, calls = setup
    responses([requests.Timeout("slow")] * 3)
    with pytest.raises(SourceError, match="three attempts"):
        client.get("https://example.org")
    assert len(calls) == 3


def test_response_size_bound(setup):
    client, responses, _, _ = setup
    responses([Response(body=b"x" * (32 * 1024 * 1024 + 1))])
    with pytest.raises(SourceError, match="32 MiB"):
        client.get("https://example.org")


def test_trickling_body_obeys_read_deadline(setup, monkeypatch):
    client, responses, _, _ = setup
    response = Response()
    now = [100.0]
    monkeypatch.setattr("litwatch.source_http.time.monotonic", lambda: now[0])

    def trickle(size, decode_content=False):
        now[0] += 29
        return b" "

    monkeypatch.setattr(response, "read1", trickle)
    responses([response])
    with pytest.raises(SourceError, match="read deadline"):
        client.get("https://example.org")
    assert now[0] < 310
