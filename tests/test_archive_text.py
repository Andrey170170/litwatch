"""A paper's full text from the archive: LaTeX first, the PDF without a usable source, the arXiv
version it is of, and nothing while the archive is still fetching."""

from __future__ import annotations

import httpx
import pytest

from litwatch.archive import (
    URGENT,
    ArchiveClient,
    ArchiveError,
    PaperText,
    TextUnavailable,
    read_text,
)
from litwatch.archive import text as text_module
from tests.conftest import paper
from tests.fakes_http import FakeArchive, mock

BODY = "\\section{Method} " + "We train a probe on every layer. " * 100


def client(fake: FakeArchive) -> ArchiveClient:
    return ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))


async def test_latex_text_of_the_papers_version_asked_for_urgently():
    fake = FakeArchive()
    fake.texts["2610.00001"] = (3, BODY)
    found = await read_text(client(fake), paper("2610.00001", version=3))
    assert found == PaperText(BODY, 3, "latex")
    assert len(found.sha256) == 64
    assert [(j["kind"], j["version"], j["priority"]) for j in fake.jobs] == [("source", 3, URGENT)]


async def test_nothing_while_the_source_is_fetched():
    fake = FakeArchive(status="pending")
    assert await read_text(client(fake), paper("2610.00001", version=1), priority=100) is None
    assert [(j["kind"], j["priority"]) for j in fake.jobs] == [("source", 100)]


async def test_a_paper_without_a_source_is_read_from_its_pdf(monkeypatch):
    fake = FakeArchive()
    fake.statuses["source"] = "unavailable"
    monkeypatch.setattr(text_module, "pdf_to_markdown", lambda pdf: BODY if pdf else None)
    found = await read_text(client(fake), paper("2610.00001", version=2))
    assert found == PaperText(BODY, 2, "pdf")
    assert [j["kind"] for j in fake.jobs] == ["source", "pdf"]
    fake.statuses["pdf"] = "pending"
    assert await read_text(client(fake), paper("2610.00001", version=2)) is None


async def test_a_source_too_short_to_be_a_paper_falls_back_to_the_pdf(monkeypatch):
    fake = FakeArchive()
    fake.texts["2610.00001"] = (1, "\\documentclass{article} stub")
    monkeypatch.setattr(text_module, "pdf_to_markdown", lambda pdf: BODY)
    assert (await read_text(client(fake), paper("2610.00001", version=1))).source == "pdf"


async def test_no_text_to_be_had_says_why(monkeypatch):
    fake = FakeArchive(status="failed")
    with pytest.raises(TextUnavailable, match="source failed; PDF failed"):
        await read_text(client(fake), paper("2610.00001", version=1))
    fake = FakeArchive()
    fake.statuses["source"] = "unavailable"
    monkeypatch.setattr(text_module, "pdf_to_markdown", lambda pdf: None)  # no `pdf` extra
    with pytest.raises(TextUnavailable, match="no usable text"):
        await read_text(client(fake), paper("2610.00001", version=1))


async def test_a_paper_of_no_known_version_reads_the_archives_latex():
    fake = FakeArchive()
    fake.texts["2610.00001"] = (None, BODY)  # the historical LaTeX
    found = await read_text(client(fake), paper("2610.00001"))
    assert (found.version, found.source) == (None, "latex")
    assert fake.jobs == []  # nothing to fetch without a version
    with pytest.raises(TextUnavailable, match="no arXiv version known"):
        await read_text(client(fake), paper("2610.00002"))


async def test_text_of_another_version_than_asked_is_refused():
    fake = FakeArchive()
    fake.texts["2610.00001"] = (2, BODY)
    archive = client(fake)
    assert (await archive.latex("2610.00001")).version == 2
    assert await archive.latex("2610.00001", 1) is None

    def wrong(req):
        return httpx.Response(200, json={"version": 1, "text": BODY})

    with pytest.raises(ArchiveError, match="another version"):
        await ArchiveClient("http://a.test", "t", http=mock(wrong)).latex("2610.00001", 2)
