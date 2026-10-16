"""A paper's full text from the archive: LaTeX first, the PDF without a usable source, the arXiv
version it is of, and nothing while the archive is still fetching."""

from __future__ import annotations

import sys
from types import SimpleNamespace

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


def a_pdf(*lines: str) -> bytes:
    """A one-page PDF with `lines` set in Helvetica, built by hand."""
    shown = b" ".join(b"(" + line.encode() + b") Tj 0 -16 Td" for line in lines)
    content = b"BT /F1 12 Tf 72 720 Td " + shown + b" ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_a_pdf_is_read_without_the_pdf_extra(monkeypatch):
    # The owner, 2026-10-15: pypdfium2 by default, so the MIT template reads PDF-only papers.
    monkeypatch.setitem(sys.modules, "pymupdf4llm", None)  # the extra not installed
    text = text_module.pdf_to_markdown(a_pdf("We train a probe.", "Accuracy is 0.81."))
    assert text == "We train a probe.\nAccuracy is 0.81."
    assert text_module.pdf_to_markdown(b"%PDF-1.4 not really") is None


def test_the_pdf_extra_is_preferred_when_installed(monkeypatch):
    class Doc:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setitem(sys.modules, "pymupdf", SimpleNamespace(open=lambda **_: Doc()))
    monkeypatch.setitem(sys.modules, "pymupdf4llm", SimpleNamespace(to_markdown=lambda d: "# M"))
    assert text_module.pdf_to_markdown(a_pdf("Plain.")) == "# M"


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


async def test_an_empty_source_text_falls_back_to_the_pdf(monkeypatch):
    fake = FakeArchive()
    fake.texts["2610.00001"] = (1, "\n\n")
    monkeypatch.setattr(text_module, "pdf_to_markdown", lambda pdf: BODY)
    assert (await read_text(client(fake), paper("2610.00001", version=1))).source == "pdf"


async def test_a_short_paper_is_still_a_paper():
    fake = FakeArchive()
    fake.texts["2610.00001"] = (1, "\\section{Erratum} Table 2 should read 0.81, not 0.18.")
    assert (await read_text(client(fake), paper("2610.00001", version=1))).source == "latex"


async def test_no_text_to_be_had_says_why(monkeypatch):
    fake = FakeArchive(status="failed")
    with pytest.raises(TextUnavailable, match="source failed; PDF failed"):
        await read_text(client(fake), paper("2610.00001", version=1))
    fake = FakeArchive()
    fake.statuses["source"] = "unavailable"
    monkeypatch.setattr(text_module, "pdf_to_markdown", lambda pdf: None)  # no text in it
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


async def test_a_paper_of_no_known_version_is_never_read_from_a_versioned_source():
    fake = FakeArchive()
    fake.texts["2610.00001"] = (3, BODY)  # v3, which the paper's metadata may not be
    with pytest.raises(TextUnavailable, match="no arXiv version known"):
        await read_text(client(fake), paper("2610.00001"))


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
