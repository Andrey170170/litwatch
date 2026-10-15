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
    ArchiveUnavailable,
    ArchiveVersionUnknown,
    PaperText,
)
from litwatch.core import Paper

log = logging.getLogger(__name__)

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
    return text if text is not None and text.body.strip() else None


def _why(job: dict) -> str:
    return str(job.get("error") or job.get("status"))[:200]


async def _retried(archive: ArchiveClient, job: dict) -> bool:
    """Ask the archive to try a failed job again; False when it is not failed (unavailable
    means arXiv has no such file) or another retry came first."""
    if job.get("status") != "failed":
        return False
    try:
        await archive.retry(job["id"])
    except ArchiveUnavailable:
        raise
    except ArchiveError:
        return False
    return True


async def retry_downloads(archive: ArchiveClient, paper: Paper, priority: int = URGENT) -> int:
    """Ask the archive to try again the downloads of the paper's text that it gave up on after
    failing: its source, and its PDF when `read_text` would fall back to it (the source not to
    be had, or without LaTeX text). Returns how many."""
    try:
        paper = await archive.resolve(paper)
    except ArchiveVersionUnknown:
        return 0  # read from the historical LaTeX alone: nothing to download
    assert paper.version is not None
    jobs = [source := await archive.request(paper, "source", priority)]
    if source.get("status") in DONE or (
        source.get("status") == "complete"
        and not _usable(await archive.latex(paper.id, paper.version))
    ):
        jobs.append(await archive.request(paper, "pdf", priority))
    return sum([await _retried(archive, job) for job in jobs])


async def read_text(
    archive: ArchiveClient, paper: Paper, priority: int = URGENT
) -> PaperText | None:
    """The paper's text, of its arXiv version, or None while the archive is still fetching it.
    Its source is asked for at `priority`; when arXiv has no usable source for it, its PDF.
    A paper of no known version is read from the archive's historical LaTeX alone, of no known
    version either: a newer source the archive holds may be of another text. Raises
    TextUnavailable when there is no text to be had, and ArchiveUnavailable while the archive
    cannot be reached."""
    try:
        paper = await archive.resolve(paper)
    except ArchiveVersionUnknown:
        if found := _usable(await archive.latex(paper.id, 0)):
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
