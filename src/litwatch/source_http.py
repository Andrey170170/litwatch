"""Paced, bounded HTTP access shared by the official arXiv metadata sources."""

import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import requests
from arxiv_archive.pacing import PacingError
from urllib3.exceptions import HTTPError as UrllibError
from urllib3.exceptions import SSLError as UrllibSSLError

from litwatch.arxiv_access import sync_response


class SourceError(RuntimeError):
    """A source could not provide a complete, valid response."""


def _retry_after(value: str) -> float:
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class XmlClient:
    """Serial client: three-second pacing, three attempts, and bounded streamed bodies.

    The 180-second deadline is checked between reads; an in-flight socket operation
    can overrun it by at most its 30-second read timeout.
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "Litwatch/0.8 (arXiv metadata reader)"
        self._session.headers["Accept-Encoding"] = "identity"
        self._last_request: float | None = None

    def close(self):
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get(self, url: str, params: dict | None = None) -> bytes:
        deadline = time.monotonic() + 180
        for attempt in range(3):
            cooldown = 0.0
            if self._last_request is not None:
                time.sleep(max(0, 3 - (time.monotonic() - self._last_request)))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceError("XML request exceeded its time budget")
            self._last_request = time.monotonic()
            try:
                with sync_response(
                    self._session,
                    url,
                    pacing_timeout=remaining,
                    params=params,
                    timeout=(min(10, remaining), min(30, remaining)),
                    stream=True,
                ) as response:
                    cooldown = _retry_after(response.headers.get("Retry-After", ""))
                    if response.status_code == 429 or 500 <= response.status_code < 600:
                        raise requests.ConnectionError(f"HTTP {response.status_code}")
                    if response.status_code != 200:
                        raise SourceError(f"XML source returned HTTP {response.status_code}")
                    if response.headers.get("Content-Encoding", "identity") != "identity":
                        raise SourceError("XML source ignored identity content encoding")
                    body = bytearray()
                    # read1 returns after one socket read, so a trickling response
                    # cannot hide the deadline while filling a large chunk.
                    while chunk := response.raw.read1(65536, decode_content=False):
                        if time.monotonic() >= deadline:
                            raise SourceError("XML response exceeded its read deadline")
                        body.extend(chunk)
                        if len(body) > 32 * 1024 * 1024:
                            raise SourceError("XML response exceeds 32 MiB")
                    ET.fromstring(body)
                    return bytes(body)
            except (requests.exceptions.SSLError, UrllibSSLError) as exc:
                raise SourceError("XML source TLS verification failed") from exc
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
                ET.ParseError,
                UrllibError,
            ) as exc:
                if attempt == 2:
                    raise SourceError(f"XML source failed after three attempts: {exc}") from exc
                delay = max((10, 30)[attempt], cooldown)
                # Honor the entire Retry-After or stop; never shorten a server cooldown.
                if time.monotonic() + delay + 10 >= deadline:
                    raise SourceError("XML source retry would exceed its time budget") from exc
                time.sleep(delay)
            except PacingError as exc:
                raise SourceError(f"Shared arXiv pacing unavailable: {exc}") from exc
            except requests.RequestException as exc:
                raise SourceError(f"XML request failed: {exc}") from exc
        raise AssertionError("unreachable")
