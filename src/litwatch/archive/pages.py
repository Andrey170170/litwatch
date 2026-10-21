"""A PDF page as an image, named by its number or by a caption on it ("Figure 3", "Table 2"),
for the figures and tables a paper's text does not show (mcp.md, Pages). PDFium renders it and
Pillow encodes it, neither of them the AGPL `pdf` extra. Under the text reader's lock, as PDFium
is not thread-safe."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import pypdfium2
from PIL import Image

from litwatch.archive.text import _PDF_LOCK

MAX_SIDE = 1568  # the long side of a page's image, in pixels
_KINDS = {"figure": r"(?:Figure|Fig\b\.?)", "table": r"(?:Table|Tab\b\.?)"}
# A caption's label: a number, an appendix's or supplement's ("A.1", "A1", "S3", "B.2.1"), a
# Roman numeral ("IV", as IEEE numbers tables) or a single letter ("A").
_LABEL = r"(?:[A-Za-z]\.?)?\d+(?:\.\d+)*|[IVXLC]+|[A-Za-z]"
_ASKED = re.compile(r"\s*(fig(?:ure)?|tab(?:le)?)\.?\s*(" + _LABEL + r")\s*", re.IGNORECASE)


class NoSuchPage(LookupError):  # noqa: N818 - an answer, as LookupError
    """The PDF has no page by that number, or none that names the caption."""


class UnreadablePdf(ValueError):  # noqa: N818 - an outcome, as ArchiveUnavailable
    """PDFium cannot open or render the PDF."""


@dataclass(frozen=True)
class Caption:
    """A figure's or a table's caption to look for, as "Figure 3" or "Table A.2" names it."""

    kind: str  # "figure" or "table"
    label: str  # "3", "A.2"

    @classmethod
    def parse(cls, asked: str) -> Caption:
        found = _ASKED.fullmatch(asked)
        if found is None:
            raise ValueError(f'a caption is named as "Figure 3" or "Table 2", not {asked!r}')
        kind = "figure" if found[1].lower().startswith("fig") else "table"
        return cls(kind, found[2])

    def __str__(self) -> str:
        return f"{self.kind.capitalize()} {self.label}"

    def patterns(self) -> tuple[re.Pattern[str], ...]:
        """Where it may be, best first: a line starting with it and a colon or full stop, a line
        starting with it, any mention. Its label matches whole ("IV" is not "XIV" or "IVa")."""
        # A lettered number matches with its dot or without ("A.1" and "A1").
        lettered = re.fullmatch(r"([A-Za-z])\.?(\d.*)", self.label)
        label = (
            re.escape(lettered[1]) + r"\.?" + re.escape(lettered[2])
            if lettered
            else re.escape(self.label)
        )
        named = _KINDS[self.kind] + r"\s*" + label + r"(?!\.?\w)"
        flags = re.IGNORECASE | re.MULTILINE
        return (
            re.compile(r"^[ \t]*" + named + r"\s*[:.]", flags),
            re.compile(r"^[ \t]*" + named, flags),
            re.compile(r"\b" + named, flags),
        )


@dataclass(frozen=True)
class Rendered:
    """A page's image: its number in the PDF (from 1), how many pages the PDF has, the PNG,
    and for a caption, whether the page holds it (else it is the first page to name it)."""

    number: int
    pages: int
    png: bytes
    captioned: bool | None = None


def render(pdf: bytes, number: int | None = None, caption: Caption | None = None) -> Rendered:
    """Page `number` of `pdf`, or the page `caption` is on (the first that names it, without a
    caption line), as a PNG whose long side is at most `MAX_SIDE` pixels. Raises `NoSuchPage`
    and `UnreadablePdf`. One PDF is read at a time."""
    with _PDF_LOCK:
        try:
            doc = pypdfium2.PdfDocument(pdf)
        except pypdfium2.PdfiumError as exc:
            raise UnreadablePdf(str(exc)) from exc
        try:
            return _render(doc, number, caption)
        except pypdfium2.PdfiumError as exc:
            raise UnreadablePdf(str(exc)) from exc
        finally:
            doc.close()


def _render(doc: pypdfium2.PdfDocument, number: int | None, caption: Caption | None) -> Rendered:
    pages = len(doc)
    captioned = None
    if caption is not None:
        number, captioned = _find(doc, caption)
    assert number is not None
    if not 1 <= number <= pages:
        raise NoSuchPage(f"the PDF has {pages} pages, so no page {number}")
    page = doc[number - 1]
    try:
        width, height = page.get_size()
        if min(width, height) <= 0:
            raise UnreadablePdf(f"page {number} has no size")
        image = page.render(scale=MAX_SIDE / max(width, height)).to_pil()
    finally:
        page.close()
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)  # rounding up past it
    out = io.BytesIO()
    image.save(out, format="PNG")
    return Rendered(number, pages, out.getvalue(), captioned)


def _find(doc: pypdfium2.PdfDocument, caption: Caption) -> tuple[int, bool]:
    """The first page holding `caption`'s line at its best rank (`Caption.patterns`), and
    whether that is a caption line (else it is the first page that names it)."""
    patterns = caption.patterns()
    found: list[int | None] = [None] * len(patterns)  # by rank, the first page
    for i in range(len(doc)):
        page = doc[i]
        try:
            text = page.get_textpage().get_text_bounded().replace("\r\n", "\n")
        finally:
            page.close()
        for rank, pattern in enumerate(patterns):
            if found[rank] is None and pattern.search(text):
                found[rank] = i + 1
        if found[0] is not None:
            break
    best = next((rank for rank, page in enumerate(found) if page is not None), None)
    if best is None:
        raise NoSuchPage(f"none of its {len(doc)} pages names {caption}")
    return found[best], best < len(patterns) - 1
