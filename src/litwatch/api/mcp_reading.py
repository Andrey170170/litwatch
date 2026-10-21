"""What the MCP server's reading tools read (mcp.md, Reading and Pages): a paper's text, whole
as `archive.read_text` gives it (none of the Brief's trimming), its outline, a part of it at most
`PAGE_CHARS` long, and its PDF. The archive is asked at urgent priority and waited on a bounded
time: past it, `StillFetching`; a paper it cannot get is `Unobtainable`."""

from __future__ import annotations

import asyncio
import bisect
import re
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


@dataclass(frozen=True)
class Part:
    """One entry of a text's outline: its index (a LaTeX heading's place, from 1, with 0 for
    what comes before the first; a PDF page's number), title, depth and span in the text, which
    runs to the next entry of its depth or above."""

    index: int
    title: str
    depth: int
    start: int
    end: int


LEVELS = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}
# A sectioning command, its optional argument and title read after it (`_group`).
_HEADING = re.compile(r"\\(" + "|".join(LEVELS) + r")(?![A-Za-z@])\*?\s*")
_BIBLIOGRAPHY = re.compile(r"\\begin\s*\{thebibliography\}")
# A TeX comment: `%` after an even run of backslashes (`\%` is a per cent sign), to the line's end.
_COMMENT = re.compile(r"(?<!\\)(?:\\\\)*%.*")
# What no heading is read in, whichever opens first: a comment, or literal text where a heading
# is an example (a verbatim-like environment, `\verb`).
_MASKED = re.compile(
    r"(?<!\\)(?:\\\\)*%[^\n]*"
    r"|\\begin\s*\{(verbatim|Verbatim|lstlisting|minted)(\*?)\}[\s\S]*?\\end\s*\{\1\2\}"
    r"|\\verb\*?([^A-Za-z\s*])[^\n]*?\3"
)
TITLE_CHARS = 200  # the most of a heading's title an outline shows
_TITLE_SCAN = 2_000  # how far a title's closing brace is looked for


def _close(text: str, start: int, closer: str) -> int | None:
    """Where the group opening at `start` closes with `closer` (`}` or `]`), past brace groups
    and escaped characters; None when it does not within `_TITLE_SCAN` characters."""
    depth, i = 0, start + 1
    stop = min(len(text), start + _TITLE_SCAN)
    while i < stop:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == closer and depth == 0:
            return i
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return None


def _group(text: str, at: int) -> int | None:
    """Where a heading's title opens, at `at` or past an optional argument there (`[...]`, which
    may hold `]` inside braces); None when no title follows."""
    if text.startswith("[", at):
        end = _close(text, at, "]")
        if end is None:
            return None
        at = end + 1
        while at < len(text) and text[at].isspace():
            at += 1
    return at if text.startswith("{", at) else None


def _title(text: str, start: int) -> str | None:
    """The braced group opening at `start`, comments dropped and spaces collapsed; None when it
    does not close within `_TITLE_SCAN` characters."""
    end = _close(text, start, "}")
    if end is None:
        return None
    inner = _COMMENT.sub("", text[start + 1 : end])
    return " ".join(inner.split())[:TITLE_CHARS] or "(untitled)"


def outline(text: PaperText) -> list[Part]:
    """The text's outline: a PDF's text by page, every page as the PDF numbers them; LaTeX by
    its sectioning commands, `\\part` to `\\subparagraph` (starred too; none in a comment or
    in verbatim text), and its bibliography."""
    body = text.body
    if text.source == "pdf":
        spans = text.pages if text.pages is not None else ((0, len(body)),)
        return [Part(n, f"Page {n}", 0, a, b) for n, (a, b) in enumerate(spans, 1)]
    masked = [(m.start(), m.end()) for m in _MASKED.finditer(body)]  # in order, apart
    starts = [a for a, _ in masked]

    def masked_at(at: int) -> bool:
        i = bisect.bisect_right(starts, at) - 1
        return i >= 0 and masked[i][0] <= at < masked[i][1]

    found: list[tuple[int, int, str]] = []  # (offset, level, title)
    for m in _HEADING.finditer(body):
        if masked_at(m.start()) or (opens := _group(body, m.end())) is None:
            continue
        if (title := _title(body, opens)) is not None:
            found.append((m.start(), LEVELS[m[1]], title))
    top = min((level for _, level, _ in found), default=LEVELS["section"])
    for m in _BIBLIOGRAPHY.finditer(body):
        if not masked_at(m.start()):
            found.append((m.start(), top, "References (the bibliography)"))
    found.sort()
    parts = []
    if not found or found[0][0] > 0:
        before = "Before the first heading" if found else "The whole text (it has no headings)"
        parts.append(Part(0, before, 0, 0, found[0][0] if found else len(body)))
    # Each entry ends where the next of its level or above starts: found from the end back.
    ends, nearest = [len(body)] * len(found), {}  # level: the nearest start after, at it
    for i in range(len(found) - 1, -1, -1):
        start, level, _ = found[i]
        ends[i] = min((at for lv, at in nearest.items() if lv <= level), default=len(body))
        nearest[level] = start
    for i, ((start, level, title), end) in enumerate(zip(found, ends, strict=True)):
        parts.append(Part(i + 1, title, level - top, start, end))
    return parts


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
