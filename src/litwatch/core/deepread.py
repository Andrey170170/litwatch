"""The Deep read as litwatch reads it (docs/specs/deep-read.md): what the deep service's result
must be to be stored, and how a stored read is taken apart by what exports it.

The read's schema is the deep service's (`deep/src/read.ts`), which validates a read before it
returns one. The job that returns it is a contract the service declares (`core.contracts.deep`,
generated): its result is told by its format tag alone, and its figures by their name and type.
litwatch stores the read as JSON (`received` checks what the contract cannot: the figures' bytes),
so what reads a stored one here takes a missing field as absent and never raises on it. A Deep
card from before the read has no format tag and none of a read's fields.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from litwatch.core.contracts import deep as contract

# A read's format tag; a Deep card from before has none.
FORMAT: contract.ReadFormat = "read"
# The process note the service leaves on a read that still failed its review.
REVIEW: contract.NoteKind = "review"
# The most one read's figures may weigh, decoded: far above any read's (about 1 MB).
FIGURES_BYTES = 32 * 1024 * 1024
# Whose a limitation is (its `by`), as every export of a read words it.
LIMITATION_BY: dict[contract.LimitationBy, str] = {"paper": "the paper's own", "read": "the read's"}


@dataclass(frozen=True)
class Figure:
    """One of a paper's figures, stored with its read under the name the read cites. Its type
    is the contract's one (WebP): the figure endpoint serves it to the owner's browser, so
    nothing a browser would run is let in."""

    name: str
    mime: str
    data: bytes


def is_read(card: Mapping[str, Any]) -> bool:
    """Whether a stored card is a Deep read; a Deep card from before is not."""
    return card.get("format") == FORMAT


def received(figures: list[contract.Figure] | None) -> list[Figure]:
    """Check the figures of a finished job, which the contract has named and typed, before any
    of them is stored: each is base64 of a WebP image, no two share a name, and together they
    weigh no more than FIGURES_BYTES. Returns them decoded; raises `ValueError` saying what is
    wrong."""
    found: dict[str, Figure] = {}
    size = 0
    for figure in figures or []:
        name = figure.name
        if name in found:
            raise ValueError(f"two figures are named {name}")
        try:
            image = base64.b64decode(figure.data, validate=True)
        except ValueError as exc:
            raise ValueError(f"figure {name} is not base64: {exc}") from exc
        if image[:4] != b"RIFF" or image[8:12] != b"WEBP":
            raise ValueError(f"figure {name} is not a WebP image")
        size += len(image)
        if size > FIGURES_BYTES:
            raise ValueError(f"the figures weigh more than {FIGURES_BYTES} bytes")
        found[name] = Figure(name, figure.mime, image)
    return list(found.values())


# -- a stored read ---------------------------------------------------------------------------------


def sections(read: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The read's sections, in order."""
    return [s for s in read.get("sections") or [] if isinstance(s, dict)]


def section(read: Mapping[str, Any], kind: str) -> dict[str, Any] | None:
    """The read's first section of a kind (`glance`, `claims`, `limits`, ...), if it has one."""
    return next((s for s in sections(read) if s.get("kind") == kind), None)


def review_note(read: Mapping[str, Any]) -> dict[str, Any] | None:
    """The process note saying the read still failed its review, if it carries one."""
    notes = read.get("process_notes") or []
    return next((n for n in notes if isinstance(n, dict) and n.get("kind") == REVIEW), None)


# -- inline text -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """A stretch of a read's inline text in one style. `tex`: `text` is inline maths, without
    its dollar signs."""

    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    tex: bool = False
    link: str | None = None


# The web app's rule (`web/src/deepread/inline.tsx`), mark for mark: an escaped mark, `code`,
# $tex$, **bold**, *italic*, [words](url). Keep the two in step.
_MARK = re.compile(
    r"\\([\\*`$\[\]])"
    r"|`([^`\n]+)`"
    r"|\$(?!\s)((?:\\.|[^$\\])+?)(?<!\s)\$(?!\d)"
    r"|\*\*(?!\s)(.+?)(?<!\s)\*\*"
    r"|\*(?![\s*])(.+?)(?<![\s*])\*"
    r"|\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)"
)


def inline(text: str, style: Run | None = None) -> list[Run]:
    """A read's inline text as runs: **bold**, *italic*, `code`, $tex$ and [words](https://…),
    and nothing else; anything else, HTML included, stays as written."""
    style = style or Run("")
    out: list[Run] = []

    def add(run: Run) -> None:
        # Two formulas side by side stay two.
        if out and not run.tex and replace(out[-1], text="") == replace(run, text=""):
            out[-1] = replace(out[-1], text=out[-1].text + run.text)
        elif run.text:
            out.append(run)

    at = 0
    for m in _MARK.finditer(text):
        add(replace(style, text=text[at : m.start()]))
        escaped, code, tex, bold, italic, words, url = m.groups()
        if escaped is not None:
            add(replace(style, text=escaped))
        elif code is not None:
            add(replace(style, text=code, code=True))
        elif tex is not None:
            add(replace(style, text=tex, tex=True))
        else:
            inner = (
                (bold, replace(style, bold=True))
                if bold is not None
                else (italic, replace(style, italic=True))
                if italic is not None
                else (words, replace(style, link=url))
            )
            for run in inline(*inner):
                add(run)
        at = m.end()
    add(replace(style, text=text[at:]))
    return out
