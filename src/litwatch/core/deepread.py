"""The Deep read as litwatch reads it (docs/specs/deep-read.md): what the deep service's result
must be to be stored, and how a stored read is taken apart by what exports it.

The read's schema is the deep service's (`deep/src/read.ts`), which validates a read before it
returns one. litwatch stores it as JSON and checks only its format tag (`received`), so what
reads a stored one here takes a missing field as absent and never raises on it. A Deep card
from before has no format tag and none of a read's fields.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

# A read's format tag; a Deep card has none.
FORMAT = "read"
# The process note the service leaves on a read that still failed its review.
REVIEW = "review"
# What a figure may be. The figure endpoint serves the stored type to the owner's browser, so
# nothing a browser would run is let in.
FIGURE_MIMES = frozenset({"image/webp"})
# A figure's name, as the read's `fig` blocks name it (`deep/src/read.ts`).
FIGURE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# The most one read's figures may weigh, decoded: far above any read's (about 1 MB).
FIGURES_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class Figure:
    """One of a paper's figures, stored with its read under the name the read cites."""

    name: str
    mime: str
    data: bytes


def is_read(card: Mapping[str, Any]) -> bool:
    """Whether a stored card is a Deep read; a Deep card from before is not."""
    return card.get("format") == FORMAT


def received(
    result: Any, figures: Any = None, reviews: Any = None
) -> tuple[list[Figure], list[Any] | None]:
    """Check what the deep service returned for a finished job before any of it is stored: the
    result's format tag (absent: a Deep card; `read`: a Deep read; anything else is refused),
    its figures and its reviews. Returns the figures decoded, and the reviews as given (None:
    no review ran). Raises `ValueError` saying what is wrong with it."""
    if not isinstance(result, dict):
        raise ValueError("the result is not an object")
    if "format" in result and result["format"] != FORMAT:
        raise ValueError(f"unknown format {result['format']!r}")
    if reviews is not None and not isinstance(reviews, list):
        raise ValueError("`reviews` is not a list")
    return _figures(figures), reviews


def _figures(sent: Any) -> list[Figure]:
    if sent is None:
        return []
    if not isinstance(sent, list):
        raise ValueError("`figures` is not a list")
    found: dict[str, Figure] = {}
    size = 0
    for figure in sent:
        if not isinstance(figure, dict):
            raise ValueError("a figure is not an object")
        name, mime, data = figure.get("name"), figure.get("mime"), figure.get("data")
        if not isinstance(name, str) or not FIGURE_NAME.fullmatch(name):
            raise ValueError(f"a figure's name is not one: {name!r}")
        if name in found:
            raise ValueError(f"two figures are named {name}")
        if mime not in FIGURE_MIMES:
            raise ValueError(f"figure {name} is {mime!r}, not {' or '.join(sorted(FIGURE_MIMES))}")
        if not isinstance(data, str):
            raise ValueError(f"figure {name} has no data")
        try:
            image = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise ValueError(f"figure {name} is not base64: {exc}") from exc
        if image[:4] != b"RIFF" or image[8:12] != b"WEBP":
            raise ValueError(f"figure {name} is not a WebP image")
        size += len(image)
        if size > FIGURES_BYTES:
            raise ValueError(f"the figures weigh more than {FIGURES_BYTES} bytes")
        found[name] = Figure(name, mime, image)
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
