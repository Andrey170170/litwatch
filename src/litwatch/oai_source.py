"""Complete incremental arXiv OAI harvests, independent of search API availability."""

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime

from litwatch.models import Paper
from litwatch.source_http import SourceError, XmlClient

URL = "https://oaipmh.arxiv.org/oai"
NS = {"o": "http://www.openarchives.org/OAI/2.0/", "a": "http://arxiv.org/OAI/arXiv/"}
_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.-]*/\d{7})(?:v\d+)?\Z")


@dataclass
class OaiBatch:
    papers: list[Paper]
    checkpoint: datetime


def _required(node: ET.Element, path: str) -> str:
    value = " ".join((node.findtext(path, default="", namespaces=NS)).split())
    if not value:
        raise SourceError(f"OAI record missing {path}")
    return value


def _date(value: str) -> str:
    try:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC).isoformat()
    except ValueError as exc:
        raise SourceError(f"Invalid OAI paper date: {value}") from exc


def _paper(record: ET.Element) -> Paper | None:
    header = record.find("o:header", NS)
    if header is None:
        raise SourceError("OAI record missing header")
    if header.get("status") == "deleted":
        return None
    node = record.find("o:metadata/a:arXiv", NS)
    if node is None:
        raise SourceError("OAI record missing arXiv metadata")
    paper_id = _required(node, "a:id")
    if not _ID.fullmatch(paper_id):
        raise SourceError(f"Invalid arXiv ID: {paper_id}")
    paper_id = re.sub(r"v\d+$", "", paper_id)
    authors = []
    for author in node.findall("a:authors/a:author", NS):
        surname = _required(author, "a:keyname")
        given = author.findtext("a:forenames", "", NS).strip()
        suffix = author.findtext("a:suffix", "", NS).strip()
        authors.append(" ".join(part for part in (given, surname, suffix) if part))
    if not authors:
        raise SourceError("OAI record missing authors")
    published = _date(_required(node, "a:created"))
    updated = node.findtext("a:updated", "", NS).strip()
    categories = _required(node, "a:categories").split()
    return Paper(
        id=paper_id,
        title=_required(node, "a:title"),
        abstract=_required(node, "a:abstract"),
        authors=authors,
        categories=categories,
        # The arXiv OAI schema does not declare category order to mean primary.
        primary_category=None,
        published=published,
        updated=_date(updated) if updated else published,
        url=f"https://arxiv.org/abs/{paper_id}",
    )


def fetch_category(category: str, since: date, client: XmlClient) -> OaiBatch:
    """Return only complete harvests; `since` addresses metadata modification dates.

    Reuse the checkpoint's UTC date inclusively on the next harvest. This overlaps
    the first response day, avoiding gaps from records modified during pagination.
    """
    if not re.fullmatch(r"[a-z-]+\.[A-Za-z]+", category):
        raise SourceError(f"Unsupported OAI category: {category}")
    archive, subject = category.split(".", 1)
    original = {
        "verb": "ListRecords",
        "metadataPrefix": "arXiv",
        "from": since.isoformat(),
        "set": f"{archive}:{archive}:{subject}",
    }
    params = original.copy()
    papers: dict[str, Paper] = {}
    checkpoint = None
    seen_tokens: set[str] = set()
    restarted = False
    deadline = time.monotonic() + 15 * 60
    for _ in range(200):
        if time.monotonic() > deadline:
            raise SourceError("OAI exceeded its 15-minute category budget")
        try:
            root = ET.fromstring(client.get(URL, params=params))
        except ET.ParseError as exc:
            raise SourceError("Malformed OAI XML") from exc
        if root.tag != f"{{{NS['o']}}}OAI-PMH":
            raise SourceError("Unexpected OAI response namespace/root")
        try:
            response_date = datetime.fromisoformat(_required(root, "o:responseDate"))
            if response_date.tzinfo is None:
                raise ValueError("timezone missing")
            response_date = response_date.astimezone(UTC)
        except ValueError as exc:
            raise SourceError("Invalid OAI responseDate") from exc
        if checkpoint is None:
            checkpoint = response_date
        errors = root.findall("o:error", NS)
        if errors:
            code = errors[0].get("code")
            if code == "noRecordsMatch" and params == original:
                return OaiBatch(list(papers.values()), checkpoint)
            if code == "badResumptionToken" and not restarted and params != original:
                restarted = True
                seen_tokens.clear()
                params = original.copy()
                continue
            raise SourceError(f"OAI error {code}: {errors[0].text}")
        listing = root.find("o:ListRecords", NS)
        if listing is None:
            raise SourceError("OAI response missing ListRecords")
        records = listing.findall("o:record", NS)
        if not records:
            raise SourceError("OAI ListRecords has no records or explicit noRecordsMatch")
        for record in records:
            paper = _paper(record)
            if paper is not None:
                papers[paper.id] = paper
        token = listing.findtext("o:resumptionToken", "", NS).strip()
        if not token:
            return OaiBatch(list(papers.values()), checkpoint)
        if token in seen_tokens:
            raise SourceError("OAI repeated a resumption token")
        seen_tokens.add(token)
        params = {"verb": "ListRecords", "resumptionToken": token}
    raise SourceError("OAI exceeded the 200-page harvest bound")
