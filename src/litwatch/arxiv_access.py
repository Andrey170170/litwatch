"""Shared arXiv pacing for direct metadata, HTML and curated PDF requests.

The archive API is deliberately outside these helpers: waiting on its worker
while holding the provider lock would deadlock acquisition.
"""

import os
import time
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import urljoin, urlsplit

from arxiv_archive.pacing import PacingError, SharedPacer

_REDIRECTS = {301, 302, 303, 307, 308}


def shared_pacer() -> SharedPacer | None:
    directory = os.environ.get("ARXIV_PACING_DIR")
    if directory == "":
        raise PacingError("ARXIV_PACING_DIR must name the shared pacing mount")
    return SharedPacer(directory) if directory is not None else None


def _redirect(url: str, location: str) -> str:
    target = urljoin(url, location)
    parsed = urlsplit(target)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"arxiv.org", "export.arxiv.org", "rss.arxiv.org"}
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not location
    ):
        raise PacingError("arXiv redirect left allowed HTTPS hosts")
    return target


@contextmanager
def sync_response(session, url: str, *, pacing_timeout: float | None = None, **kwargs):
    """Stream a requests response; keep shared slot until response close."""
    pacer = shared_pacer()
    if pacer is None:
        with session.get(url, **kwargs) as response:
            yield response
        return
    deadline = None if pacing_timeout is None else time.monotonic() + pacing_timeout
    kwargs["allow_redirects"] = False
    for _attempt in range(5):
        remaining = None if deadline is None else max(0, deadline - time.monotonic())
        with pacer.slot(timeout=remaining), session.get(url, **kwargs) as response:
            pacer.defer(response.headers.get("Retry-After"))
            if response.status_code not in _REDIRECTS:
                yield response
                return
            url = _redirect(url, response.headers.get("Location", ""))
        kwargs.pop("params", None)
    raise PacingError("too many arXiv redirects")


@asynccontextmanager
async def async_response(client, method: str, url: str, **kwargs):
    """Stream an httpx response while holding shared pacing ownership."""
    pacer = shared_pacer()
    if pacer is None:
        async with client.stream(method, url, **kwargs) as response:
            yield response
        return
    kwargs["follow_redirects"] = False
    for _attempt in range(5):
        async with pacer.async_slot(), client.stream(method, url, **kwargs) as response:
            pacer.defer(response.headers.get("Retry-After"))
            if response.status_code not in _REDIRECTS:
                yield response
                return
            url = _redirect(url, response.headers.get("Location", ""))
        kwargs.pop("params", None)
    raise PacingError("too many arXiv redirects")


async def async_get(client, url: str, **kwargs):
    """Buffered GET; return readable bytes after closing the provider connection."""
    if shared_pacer() is None:
        return await client.get(url, **kwargs)
    async with async_response(client, "GET", url, **kwargs) as response:
        await response.aread()
        return response
