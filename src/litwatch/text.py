"""Paper text for the deep processor: arXiv's HTML rendering as markdown, PDF text as fallback.

The markdown keeps math as LaTeX (from LaTeXML's `alttext`), headings as `#` levels, tables as
pipe tables, and bibliography entries as `- ` list items, which is what the REPL helpers parse.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import MarkdownConverter

from litwatch.archive_client import (
    ArchiveClient,
    ArchiveError,
    ArchiveUnavailable,
    ArchiveVersionUnknown,
)
from litwatch.arxiv_access import async_get
from litwatch.models import Paper

log = logging.getLogger(__name__)

ARXIV_PACING_SECONDS = 3.0
MIN_BODY_CHARS = 2000  # anything shorter is not a paper; fall back to the PDF
_DROP = [
    ".ltx_authors",
    ".ltx_role_newpage",
    ".ltx_rdf",
    ".ltx_page_navbar",
    ".ltx_page_footer",
    ".ltx_TOC",
    "nav",
    "footer",
    "script",
    "style",
    "button",
]
_BIB_NOISE = re.compile(r"\s*(External Links:.*|Cited by:.*)$")


class _Converter(MarkdownConverter):
    """markdownify with LaTeXML-aware handling of math, citations, figures, and anchors."""

    def convert_math(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        tex = " ".join((el.get("alttext") or text).split())
        return f"$${tex}$$" if el.get("display") == "block" else f"${tex}$"

    def convert_cite(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        return text

    def convert_a(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        href = el.get("href") or ""
        return text if href.startswith("#") else super().convert_a(el, text, parent_tags)

    def convert_figcaption(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        return f"\n\n*{' '.join(text.split())}*\n\n"

    def convert_img(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        return ""


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    root = soup.find("article") or soup.body or soup
    for selector in _DROP:
        for el in root.select(selector):
            el.decompose()
    for note in root.select(".ltx_note"):
        content = note.select_one(".ltx_note_content") or note
        for mark in content.select(".ltx_note_mark, .ltx_note_type"):
            mark.decompose()
        text = " ".join(content.get_text(" ", strip=True).split())
        note.replace_with(NavigableString(f" [footnote: {text}] "))
    if (abstract := root.select_one(".ltx_abstract")) and (
        title := abstract.find(class_="ltx_title")
    ):
        title.name = "h2"
    for table in root.select("table.ltx_eqn_table"):
        _equation_block(table)
    for item in root.select(".ltx_bibitem"):
        text = _BIB_NOISE.sub("", " ".join(item.get_text(" ", strip=True).split()))
        item.string = re.sub(r"\s+([.,;:?])", r"\1", text)
    converter = _Converter(
        heading_style="ATX",
        bullets="-",
        escape_underscores=False,
        escape_asterisks=False,
        escape_misc=False,
    )
    md = converter.convert_soup(root)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


def _equation_block(table: Tag) -> None:
    """LaTeXML lays out display equations as tables; emit them as one `$$` block instead."""
    lines = []
    for row in table.select("tr"):
        tex = [" ".join(m["alttext"].split()) for m in row.select("math[alttext]")]
        if not tex:
            continue
        number = row.select_one(".ltx_eqn_eqno, .ltx_tag_equation")
        tag = f" \\tag{{{number.get_text(strip=True).strip('()')}}}" if number else ""
        lines.append(" ".join(tex) + tag)
    if lines:
        table.replace_with(NavigableString("\n\n$$\n" + " \\\\\n".join(lines) + "\n$$\n\n"))
    else:
        table.decompose()


def pdf_to_markdown(pdf: bytes) -> str:
    import pymupdf  # noqa: PLC0415 - heavy import, only on the fallback path
    import pymupdf4llm  # noqa: PLC0415

    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return pymupdf4llm.to_markdown(doc)


class BodyFetcher:
    """Prefer arXiv HTML (keeps structure, converts in milliseconds); else the archived PDF."""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        archive: ArchiveClient | None = None,
    ) -> None:
        self.archive = archive
        self.http = http or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        self._lock = asyncio.Lock()

    async def fetch(self, paper: str | Paper) -> tuple[str, str] | None:
        """Return (markdown, html/pdf), without duplicating a pending archive download."""
        paper_id = paper.id if isinstance(paper, Paper) else paper
        identity = (
            f"{paper.id}v{paper.version}"
            if isinstance(paper, Paper) and paper.version
            else paper_id
        )
        prepared = None
        direct_pdf = self.archive is None
        if self.archive is not None:
            try:
                prepared = await self.archive.prepare_pdf(paper)
                identity = f"{prepared[0].id}v{prepared[0].version}"
            except (ArchiveUnavailable, ArchiveVersionUnknown) as exc:
                log.warning("%s: %s; using paced text fallback", paper_id, type(exc).__name__)
                direct_pdf = True
            except ArchiveError:
                log.warning("%s: archive preparation failed; trying HTML", paper_id)
        html = await self._get(f"https://arxiv.org/html/{identity}")
        if html is not None:
            md = await asyncio.to_thread(html_to_markdown, html.decode("utf-8", "replace"))
            if len(md) >= MIN_BODY_CHARS:
                return md, "html"
            log.warning("%s: HTML rendering too short (%d chars), trying PDF", paper_id, len(md))
        if prepared is not None and self.archive is not None:
            try:
                pdf = await self.archive.wait_pdf(*prepared)
                return await self._convert_pdf(pdf) if pdf is not None else None
            except ArchiveUnavailable:
                direct_pdf = True
            except ArchiveError:
                log.warning("%s: archive PDF failed; retrying on next processing run", paper_id)
                return None
        if not direct_pdf:
            return None
        pdf = await self._get(f"https://arxiv.org/pdf/{identity}")
        if pdf is None or not pdf.startswith(b"%PDF-"):
            return None
        return await self._convert_pdf(pdf)

    @staticmethod
    async def _convert_pdf(pdf: bytes) -> tuple[str, str] | None:
        md = await asyncio.to_thread(pdf_to_markdown, pdf)
        return (md, "pdf") if len(md) >= MIN_BODY_CHARS else None

    async def _get(self, url: str) -> bytes | None:
        async with self._lock:
            resp = await async_get(self.http, url)
            await asyncio.sleep(ARXIV_PACING_SECONDS)
        if resp.status_code != 200:
            log.info("%s -> %s", url, resp.status_code)
            return None
        return resp.content
