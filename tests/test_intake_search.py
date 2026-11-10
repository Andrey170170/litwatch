"""An owner's arXiv lookup, through the real arxiv parser."""

import asyncio
import fcntl
import time
from urllib.parse import parse_qs

import httpx
import pytest

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
