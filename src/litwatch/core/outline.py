"""A LaTeX source's outline, the one parser of it (docs/plans/a2-work-gate.md, D3): its
sectioning commands, `\\part` to `\\subparagraph` (none in a comment or in verbatim text), its
abstract and bibliography, the number LaTeX's defaults give each section, and where its appendix
starts. The MCP server's outline lists it, and the deep job carries it to the deep service,
which reads a LaTeX paper by it."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Part:
    """One entry of a text's outline: its index (a LaTeX heading's place, from 1, with 0 for
    what comes before the first; a PDF page's number), title, depth and span in the text, which
    runs to the next entry of its depth or above. `number` is the section's number where LaTeX's
    defaults give it ("3.2", "A.1"), and `appendix` says the entry is in the appendix."""

    index: int
    title: str
    depth: int
    start: int
    end: int
    number: str | None = None
    appendix: bool = False


@dataclass(frozen=True)
class Outline:
    """A LaTeX source's outline: its parts in the order of the text, the span of its
    bibliography environment, and where its appendix starts (None for either it has none)."""

    parts: list[Part]
    references: tuple[int, int] | None
    appendix: int | None


LEVELS = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}
# A sectioning command, starred or not, its optional argument and title read after it (`_group`).
_HEADING = re.compile(r"\\(" + "|".join(LEVELS) + r")(?![A-Za-z@])(\*?)\s*")
_ABSTRACT = re.compile(r"\\begin\s*\{abstract\}")
_BIBLIOGRAPHY = re.compile(r"\\begin\s*\{thebibliography\}")
_BIBLIOGRAPHY_END = re.compile(r"\\end\s*\{thebibliography\}")
# What starts the appendix by command: from here its sections are lettered.
_APPENDIX_COMMAND = r"\\appendi(?:x|ces)(?![a-zA-Z])|\\begin\s*\{appendi(?:x|ces)\}"
# Where the appendix starts: that command, or a section named for it.
_APPENDIX = re.compile(
    _APPENDIX_COMMAND
    + r"|\\section\*?\s*(?:\[[^\]]*\])?\s*\{\s*(?:Appendi(?:x|ces)|Supplementa(?:ry|l)\b)"
)
# What numbers a source's sections some other way than an article's defaults: it sets or steps a
# counter of them, ties it to another or prints its form (\thesection); it has chapters, a class
# that has them, the package that ties counters together, or a class that numbers sections in
# Roman numerals and subsections by letter.
_COUNTERS = "section|subsection|subsubsection|secnumdepth"
_OWN_NUMBERING = re.compile(
    r"\\(?:setcounter|addtocounter|stepcounter|refstepcounter|numberwithin|counterwithin\*?"
    rf"|counterwithout\*?)\s*\{{(?:{_COUNTERS})\}}|\\the(?:{_COUNTERS})(?![a-zA-Z])"
    r"|\\chapter\*?\s*[\[{]"
    r"|\\documentclass\s*(?:\[[^\]]*\])?\s*\{(?:book|report|memoir|scrbook|scrreprt"
    r"|IEEEtran|revtex[\w-]*)\}"
    r"|\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\bchngcntr\b"
)
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


def _title(text: str, start: int) -> str:
    """The braced group opening at `start`, comments dropped and spaces collapsed; what it holds
    within `_TITLE_SCAN` characters when it does not close by then, which is still a heading."""
    end = _close(text, start, "}")
    inner = _COMMENT.sub("", text[start + 1 : start + _TITLE_SCAN if end is None else end])
    return " ".join(inner.split())[:TITLE_CHARS] or "(untitled)"


@dataclass
class _Heading:
    start: int
    level: int
    title: str
    starred: bool = True  # an abstract or a bibliography is never numbered
    number: str | None = None


def _number(found: list[_Heading], lettering: int | None) -> None:
    """Number the sections, subsections and subsubsections, in the order of the text, as
    LaTeX's defaults do: counted from 1, a starred one left bare, and lettered from `lettering`
    (the appendix command) on, which counts afresh. A subsection before any section, and an
    appendix past Z, gets none."""
    counters = [0, 0, 0]
    lettered = False
    for h in found:
        depth = h.level - LEVELS["section"]
        if h.starred or not 0 <= depth <= 2:
            continue
        if not lettered and lettering is not None and h.start > lettering:
            lettered, counters = True, [0, 0, 0]
        counters[depth] += 1
        counters[depth + 1 :] = [0] * (2 - depth)
        if counters[0] == 0 or (lettered and counters[0] > 26):
            continue
        top = chr(64 + counters[0]) if lettered else str(counters[0])
        h.number = ".".join([top, *map(str, counters[1 : depth + 1])])


def latex(body: str) -> Outline:
    """A LaTeX source's outline. Its sections are numbered only where the source leaves their
    numbering to LaTeX's defaults (`_OWN_NUMBERING`): one numbered another way is not guessed
    at, and none of its headings gets a number."""
    masked = [(m.start(), m.end()) for m in _MASKED.finditer(body)]  # in order, apart
    starts = [a for a, _ in masked]

    def masked_at(at: int) -> bool:
        i = bisect.bisect_right(starts, at) - 1
        return i >= 0 and masked[i][0] <= at < masked[i][1]

    def first(pattern: re.Pattern[str]) -> re.Match[str] | None:
        return next((m for m in pattern.finditer(body) if not masked_at(m.start())), None)

    found: list[_Heading] = []
    for m in _HEADING.finditer(body):
        if masked_at(m.start()) or (opens := _group(body, m.end())) is None:
            continue
        found.append(_Heading(m.start(), LEVELS[m[1]], _title(body, opens), starred=m[2] == "*"))
    top = min((h.level for h in found), default=LEVELS["section"])
    for pattern, title in ((_ABSTRACT, "Abstract"), (_BIBLIOGRAPHY, "References")):
        for m in pattern.finditer(body):
            if not masked_at(m.start()):
                found.append(_Heading(m.start(), top, title))
    found.sort(key=lambda h: h.start)
    if not _OWN_NUMBERING.search(_MASKED.sub("", body)):
        command = re.compile(_APPENDIX_COMMAND)
        _number(found, (m := first(command)) and m.start())

    references = None
    if opened := first(_BIBLIOGRAPHY):
        closed = _BIBLIOGRAPHY_END.search(body, opened.start())
        references = (opened.start(), closed.end() if closed else len(body))
    appendix = (m := first(_APPENDIX)) and m.start()

    parts = []
    if not found or found[0].start > 0:
        before = "Before the first heading" if found else "The whole text (it has no headings)"
        parts.append(Part(0, before, 0, 0, found[0].start if found else len(body)))
    # Each entry ends where the next of its level or above starts: found from the end back.
    ends, nearest = [len(body)] * len(found), {}  # level: the nearest start after, at it
    for i in range(len(found) - 1, -1, -1):
        h = found[i]
        ends[i] = min((at for lv, at in nearest.items() if lv <= h.level), default=len(body))
        nearest[h.level] = h.start
    for i, (h, end) in enumerate(zip(found, ends, strict=True)):
        in_appendix = appendix is not None and h.start >= appendix
        parts.append(Part(i + 1, h.title, h.level - top, h.start, end, h.number, in_appendix))
    return Outline(parts, references, appendix)
