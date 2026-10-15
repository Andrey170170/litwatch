"""A paper's full text, read from the archive (docs/specs/v1/pipeline.md, section 6): the text of
its LaTeX source first, the text of its PDF when it has no usable source. The archive is the only
downloader: reading asks it for the file at a priority, and a file it is still fetching is no
text yet, which the next read asks about again."""

from __future__ import annotations

import asyncio
import logging

from litwatch.archive.client import (
    URGENT,
    ArchiveClient,
    ArchiveError,
    ArchiveVersionUnknown,
    PaperText,
)
from litwatch.core import Paper

log = logging.getLogger(__name__)

MIN_BODY_CHARS = 500  # shorter is a stub or a scan with no text layer; an erratum is longer
DONE = ("failed", "unavailable")  # a job the archive has given up on


class TextUnavailable(ArchiveError):  # noqa: N818 - an outcome, as ArchiveUnavailable
    """The archive could get neither the paper's source nor a PDF with text in it."""


def pdf_to_markdown(pdf: bytes) -> str | None:
    """The PDF's text, or None without the `pdf` extra (pymupdf4llm, AGPL)."""
    try:
        import pymupdf  # noqa: PLC0415 - heavy and optional, only on the fallback path
        import pymupdf4llm  # noqa: PLC0415
    except ImportError:
        log.warning("no PDF text without the `pdf` extra (pip install 'litwatch[pdf]')")
        return None
    try:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            return pymupdf4llm.to_markdown(doc)
    except Exception as exc:  # a broken PDF is no text, not a reason to retry forever
        log.warning("unreadable PDF: %s", type(exc).__name__)
        return None


def _usable(text: PaperText | None) -> PaperText | None:
    return text if text is not None and len(text.body) >= MIN_BODY_CHARS else None


def _why(job: dict) -> str:
    return str(job.get("error") or job.get("status"))[:200]


async def read_text(
    archive: ArchiveClient, paper: Paper, priority: int = URGENT
) -> PaperText | None:
    """The paper's text, of its arXiv version, or None while the archive is still fetching it.
    Its source is asked for at `priority`; when arXiv has no usable source for it, its PDF.
    A paper of no known version is read from whatever LaTeX the archive holds, its version as
    the archive gives it. Raises TextUnavailable when there is no text to be had, and
    ArchiveUnavailable while the archive cannot be reached."""
    try:
        paper = await archive.resolve(paper)
    except ArchiveVersionUnknown:
        if found := _usable(await archive.latex(paper.id)):
            return found
        raise TextUnavailable(f"{paper.id}: no arXiv version known and no LaTeX text") from None
    assert paper.version is not None
    source = await archive.request(paper, "source", priority)
    if source.get("status") == "complete":
        if found := _usable(await archive.latex(paper.id, paper.version)):
            return found
    elif source.get("status") not in DONE:
        return None
    pdf = await archive.request(paper, "pdf", priority)
    if pdf.get("status") in DONE:
        raise TextUnavailable(f"{paper.id}v{paper.version}: source {_why(source)}; PDF {_why(pdf)}")
    if pdf.get("status") != "complete":
        return None
    body = await archive.available_pdf(paper.id, paper.version)
    text = await asyncio.to_thread(pdf_to_markdown, body) if body else None
    if found := _usable(PaperText(text, paper.version, "pdf") if text else None):
        return found
    raise TextUnavailable(f"{paper.id}v{paper.version}: no usable text in its source or PDF")
