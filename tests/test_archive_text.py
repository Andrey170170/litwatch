"""A paper's full text from the archive: LaTeX first, the PDF without a usable source, the arXiv
version it is of, and nothing while the archive is still fetching."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from litwatch.archive import (
    URGENT,
    ArchiveClient,
    ArchiveError,
    PaperText,
    PdfText,
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
    return pages_pdf(list(lines))


def pages_pdf(*pages: list[str]) -> bytes:
    """A PDF of `pages`, each the lines set on it in Helvetica (none: a blank page)."""
    n = len(pages)
    # 1: the catalog, 2: the page tree, 3: the font, then each page and its content.
    kids = b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(n))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % n,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, lines in enumerate(pages):
        shown = b" ".join(b"(" + line.encode() + b") Tj 0 -16 Td" for line in lines)
        content = b"BT /F1 12 Tf 72 720 Td " + shown + b" ET" if lines else b""
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << /Font << /F1 3 0 R >> >> >>" % (5 + 2 * i)
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
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
    text = text_module.read_pdf(a_pdf("We train a probe.", "Accuracy is 0.81."))
    assert text == PdfText("We train a probe.\nAccuracy is 0.81.", ((0, 35),))
    assert text_module.read_pdf(b"%PDF-1.4 not really") is None


def test_each_page_keeps_its_span_and_a_blank_one_still_counts(monkeypatch):
    # The MCP outlines a PDF's text by page, numbered as the PDF numbers them (mcp.md).
    monkeypatch.setitem(sys.modules, "pymupdf4llm", None)
    pdf = pages_pdf(["Page one."], [], ["Page three."], [])
    read = text_module.read_pdf(pdf)
    assert read.body == "Page one.\n\nPage three."  # the text a Brief reads, as before
    assert [read.body[a:b] for a, b in read.pages] == ["Page one.", "", "Page three.", ""]
    assert read.pages == ((0, 9), (9, 9), (11, 22), (22, 22))
    assert text_module.read_pdf(pages_pdf([], [])) is None  # blank pages are no text


def test_the_pdf_extra_is_preferred_when_installed(monkeypatch):
    class Doc:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setitem(sys.modules, "pymupdf", SimpleNamespace(open=lambda **_: Doc()))
    chunks = [{"text": "# M\n"}, {"text": ""}, {"text": "page 3\n"}]
    extra = SimpleNamespace(to_markdown=lambda d, page_chunks: chunks if page_chunks else None)
    monkeypatch.setitem(sys.modules, "pymupdf4llm", extra)
    spans = ((0, 4), (4, 4), (4, 11))
    assert text_module.read_pdf(a_pdf("Plain.")) == PdfText("# M\npage 3\n", spans)


def test_one_pdf_is_read_at_a_time(monkeypatch):
    # PDFium is not thread-safe, and a batch reads several papers' text in threads at once.
    reading, most = 0, 0

    class Document:
        def __init__(self, _pdf):
            nonlocal reading, most
            reading += 1
            most = max(most, reading)
            time.sleep(0.05)

        def __iter__(self):
            return iter(())

        def close(self):
            nonlocal reading
            reading -= 1

    monkeypatch.setitem(sys.modules, "pymupdf4llm", None)
    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=Document))
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(text_module.read_pdf, [b"%PDF"] * 4))
    assert most == 1


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
    monkeypatch.setattr(text_module, "read_pdf", lambda pdf: PdfText(BODY, ((0, len(BODY)),)))
    found = await read_text(client(fake), paper("2610.00001", version=2))
    assert found == PaperText(BODY, 2, "pdf", ((0, len(BODY)),))
    assert [j["kind"] for j in fake.jobs] == ["source", "pdf"]
    fake.statuses["pdf"] = "pending"
    assert await read_text(client(fake), paper("2610.00001", version=2)) is None


async def test_an_empty_source_text_falls_back_to_the_pdf(monkeypatch):
    fake = FakeArchive()
    fake.texts["2610.00001"] = (1, "\n\n")
    monkeypatch.setattr(text_module, "read_pdf", lambda pdf: PdfText(BODY, ((0, len(BODY)),)))
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
    monkeypatch.setattr(text_module, "read_pdf", lambda pdf: None)  # no text in it
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


async def test_use_the_pdf_reads_the_pdf_alone(monkeypatch):
    # DESIGN.md, Needs you: "Use the PDF", for a paper whose LaTeX reads badly.
    fake = FakeArchive()
    fake.texts["2610.00001"] = (1, BODY)  # a source that reads, badly
    monkeypatch.setattr(text_module, "read_pdf", lambda pdf: PdfText("From the PDF.", ((0, 13),)))
    found = await read_text(client(fake), paper("2610.00001", version=1), pdf=True)
    assert found == PaperText("From the PDF.", 1, "pdf", ((0, 13),))
    assert [j["kind"] for j in fake.jobs] == ["pdf"]
    fake.statuses["pdf"] = "unavailable"
    with pytest.raises(TextUnavailable, match="source not read .Use the PDF.; PDF unavailable"):
        await read_text(client(fake), paper("2610.00001", version=1), pdf=True)
    fake.texts["2610.00002"] = (None, BODY)
    with pytest.raises(TextUnavailable, match="no arXiv version known, so no PDF"):
        await read_text(client(fake), paper("2610.00002"), pdf=True)


async def test_use_the_pdf_has_the_archive_retry_the_pdf_alone():
    fake = FakeArchive(status="failed")
    one = paper("2610.00001", version=1)
    asked = await text_module.retry_downloads(client(fake), one, pdf=True)
    assert asked == 1
    assert [fake.jobs[i - 1]["kind"] for i in fake.retried] == ["pdf"]


async def test_a_pdf_is_read_in_the_executor_given(monkeypatch):
    # The MCP reads PDFs in its own executor, off the loop's default pool (mcp-server.md).
    fake = FakeArchive()
    fake.statuses["source"] = "unavailable"
    threads = []

    def read(pdf):
        threads.append(threading.current_thread().name)
        return PdfText(BODY, ((0, len(BODY)),))

    monkeypatch.setattr(text_module, "read_pdf", read)
    with ThreadPoolExecutor(1, thread_name_prefix="own-pdf") as own:
        await read_text(client(fake), paper("2610.00001", version=2), executor=own)
    await read_text(client(fake), paper("2610.00001", version=2))
    assert threads[0].startswith("own-pdf") and not threads[1].startswith("own-pdf")
