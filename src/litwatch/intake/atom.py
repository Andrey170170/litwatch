"""Daily announcements, whose dates are deliberately not canonical submission dates."""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime

from litwatch.core import Paper
from litwatch.intake.http import SourceError, XmlClient

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.\-]+/\d{7})(?:v\d+)?\Z")
_TYPES = {"new", "cross", "replace", "replace-cross"}
_PREAMBLE = re.compile(r"\AarXiv:(\S+)\s+Announce Type:\s*(\S+)\s+Abstract:\s*(.*)\Z", re.DOTALL)


@dataclass
class AtomBatch:
    papers: list[Paper]
    announcement_dates: list[date]
    raw: bytes
    generated_at: datetime
    announcements: dict[str, str]


def _text(element: ET.Element, name: str) -> str:
    node = element.find(name)
    value = "" if node is None else " ".join("".join(node.itertext()).split())
    if not value:
        raise SourceError(f"Atom feed missing {name}")
    return value


def _date(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed
    except ValueError as exc:
        raise SourceError(f"Invalid Atom timestamp: {value!r}") from exc


def _id(value: str) -> str:
    value = value.removeprefix("oai:arXiv.org:")
    if not _ID.fullmatch(value):
        raise SourceError(f"Invalid Atom arXiv id: {value!r}")
    return re.sub(r"v\d+$", "", value)


def parse_feed(raw: bytes) -> AtomBatch:
    """Validate a whole feed before ingestion; duplicate conflicts invalidate the snapshot.

    dc:creator is a display credit, not a lossless list of individual authors. Keep
    it intact as one author until canonical harvesting supplies structured names.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceError("Malformed Atom XML") from exc
    if root.tag != _ATOM + "feed":
        raise SourceError("Expected an Atom feed")
    generated_at = _date(_text(root, _ATOM + "updated"))
    entries = root.findall(_ATOM + "entry")
    if len(entries) >= 2000:
        raise SourceError("Atom feed reached 2000 entries; possible truncation")
    papers: dict[str, Paper] = {}
    announcements: dict[str, str] = {}
    dates: dict[str, date] = {}
    for entry in entries:
        raw_id = _text(entry, _ATOM + "id")
        paper_id = _id(raw_id)
        summary = _text(entry, _ATOM + "summary")
        preamble = _PREAMBLE.fullmatch(summary)
        if preamble is None or _id(preamble[1]) != paper_id:
            raise SourceError(f"Invalid Atom abstract preamble for {paper_id}")
        announcement = _text(entry, _ARXIV + "announce_type")
        if announcement not in _TYPES or announcement != preamble[2]:
            raise SourceError(f"Invalid Atom announcement type for {paper_id}")
        abstract = preamble[3].strip()
        if not abstract:
            raise SourceError(f"Missing Atom abstract for {paper_id}")
        published = _date(_text(entry, _ATOM + "published")).date()
        categories = [node.get("term", "").strip() for node in entry.findall(_ATOM + "category")]
        if not categories or not all(categories):
            raise SourceError(f"Missing Atom categories for {paper_id}")
        paper = Paper(
            id=paper_id,
            version=(int(m.group(1)) if (m := re.search(r"v([1-9]\d*)$", raw_id)) else None),
            title=_text(entry, _ATOM + "title"),
            abstract=abstract,
            authors=[_text(entry, _DC + "creator")],
            categories=list(dict.fromkeys(categories)),
            url=f"https://arxiv.org/abs/{paper_id}",
        )
        if paper_id in papers and (
            papers[paper_id] != paper
            or announcements[paper_id] != announcement
            or dates[paper_id] != published
        ):
            raise SourceError(f"Conflicting Atom entries for {paper_id}")
        papers[paper_id] = paper
        announcements[paper_id] = announcement
        dates[paper_id] = published
    return AtomBatch(
        papers=list(papers.values()),
        announcement_dates=sorted(set(dates.values())),
        raw=raw,
        generated_at=generated_at,
        announcements=announcements,
    )


def fetch_category(category: str, client: XmlClient) -> AtomBatch:
    """Fetch one category using the caller's shared, serial, bounded transport."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]*", category):
        raise SourceError(f"Invalid Atom category: {category!r}")
    return parse_feed(client.get(f"https://rss.arxiv.org/atom/{category}"))
