"""What the MCP server's reading tools read (mcp.md, Reading and Pages): a paper's text, whole
as `archive.read_text` gives it (none of the Brief's trimming), its outline, a part of it at most
`PAGE_CHARS` long, and its PDF. The archive is asked at urgent priority and waited on a bounded
time: past it, `StillFetching`; a paper it cannot get is `Unobtainable`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor
from dataclasses import dataclass

from litwatch.archive import (
    URGENT,
    ArchiveClient,
    ArchiveError,
    ArchiveVersionUnknown,
    PaperText,
    TextUnavailable,
    read_text,
)
from litwatch.archive.text import DONE, WAITING, job_status
from litwatch.core import Paper
from litwatch.core.outline import Part, latex

WAIT_SECONDS = 30.0  # how long a call waits on the archive for a paper it is fetching
POLL_SECONDS = 2.0
# Past the wait, the time left to read a file that became ready at its end; also the bound on
# a call that got no answer at all.
READ_SLACK = 10.0
# The most one answer holds: about 18k tokens of math-heavy LaTeX (some 2.2 characters a token,
# fulltext/brief.py), under Claude Code's cap with room for the answer's own lines.
PAGE_CHARS = 40_000


class StillFetching(Exception):  # noqa: N818 - an outcome
    """The archive is still fetching the paper's file past the wait."""


class Unobtainable(Exception):  # noqa: N818 - an outcome
    """The archive cannot get the paper's file; the message says why."""


async def _polled[T](step: Callable[[], Awaitable[T | None]], wait: float) -> T:
    """`step`'s answer, asked again every `POLL_SECONDS` while it is None (the archive said it
    is fetching), for `wait` seconds; past them `StillFetching`. The whole is bounded by
    `wait + READ_SLACK`: a step that does not answer in it, whatever came before, is a
    `TimeoutError`, as nothing says the archive is still fetching."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    async with asyncio.timeout(wait + READ_SLACK):
        while (found := await step()) is None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise StillFetching
            await asyncio.sleep(min(POLL_SECONDS, remaining))
        return found


async def text_of(
    archive: ArchiveClient, paper: Paper, *, pdf: bool, executor: Executor, wait: float
) -> PaperText:
    """The paper's text, its PDF's alone with `pdf` (the owner's "Use the PDF"), a PDF read in
    `executor`. Raises `StillFetching`, `Unobtainable`, `TimeoutError` and `ArchiveError`."""
    try:
        return await _polled(
            lambda: read_text(archive, paper, URGENT, pdf=pdf, executor=executor), wait
        )
    except TextUnavailable as exc:
        raise Unobtainable(str(exc)) from exc


async def pdf_of(archive: ArchiveClient, paper: Paper, *, wait: float) -> tuple[Paper, bytes]:
    """The paper, of its arXiv version, and its PDF: asked for at urgent priority and its job
    polled. Raises as `text_of`."""
    known: Paper | None = None
    job: dict | None = None

    async def step() -> bytes | None:
        nonlocal known, job
        if known is None:
            try:
                known = await archive.resolve(paper)
            except ArchiveVersionUnknown:
                why = "no arXiv version of it is known, and PDFs are kept by version"
                raise Unobtainable(why) from None
        assert known.version is not None
        job = await (
            archive.request(known, "pdf", URGENT) if job is None else archive.job(job["id"])
        )
        if (status := job_status(job)) in DONE:
            raise Unobtainable(f"its PDF {str(job.get('error') or status)[:200]}")
        if status in WAITING:
            return None
        body = await archive.available_pdf(known.id, known.version)
        if body is None:
            raise ArchiveError("the archive's PDF job is complete but it serves no PDF")
        return body

    body = await _polled(step, wait)
    assert known is not None
    return known, body


# -- the outline -----------------------------------------------------------------------------------


def outline(text: PaperText) -> list[Part]:
    """The text's outline: a PDF's text by page, every page as the PDF numbers them; LaTeX by
    its sectioning commands, numbered, and its abstract and bibliography (`core.outline`)."""
    if text.source == "pdf":
        spans = text.pages if text.pages is not None else ((0, len(text.body)),)
        return [Part(n, f"Page {n}", 0, a, b) for n, (a, b) in enumerate(spans, 1)]
    return latex(text.body).parts


# -- a part of the text ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """What one answer reads: `start` to `end`, of a part or the text that runs to `stop`."""

    start: int
    end: int
    stop: int


def window(size: int, part: Part | None, offset: int | None, length: int | None) -> Window:
    """The characters to read: from `offset` (else the part's start, else 0) for `length` (at
    most `PAGE_CHARS`), never past the part's end. Raises `ValueError` for an offset outside
    the part or the text."""
    low, stop = (part.start, part.end) if part is not None else (0, size)
    start = low if offset is None else offset
    if not low <= start <= stop:
        where = (
            f"section {part.index} ({low}–{stop})" if part is not None else f"the text (0–{size})"
        )
        raise ValueError(f"offset {start} is outside {where}")
    asked = PAGE_CHARS if length is None else min(length, PAGE_CHARS)
    return Window(start, min(stop, start + asked), stop)
