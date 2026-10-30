"""Complete incremental arXiv OAI harvests, independent of search API availability."""

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime

from litwatch.core import Paper
from litwatch.intake.http import SourceError, XmlClient

URL = "https://oaipmh.arxiv.org/oai"
NS = {
    "o": "http://www.openarchives.org/OAI/2.0/",
    "a": "http://arxiv.org/OAI/arXiv/",
    "r": "http://arxiv.org/OAI/arXivRaw/",
}
ALL = "*"  # every archive and category: a harvest with no set
_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.-]*/\d{7})(?:v\d+)?\Z")


class OverBudget(SourceError):  # noqa: N818 - a harvest too large for one call
    """A harvest needed more than its pages or its time; a smaller window may fit."""


@dataclass
class OaiBatch:
    papers: list[Paper]
    checkpoint: datetime
    versions_error: str | None = None


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
        doi=node.findtext("a:doi", None, NS),
        journal_ref=node.findtext("a:journal-ref", None, NS),
        license=node.findtext("a:license", None, NS),
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


def _harvest(
    category: str,
    since: date,
    client: XmlClient,
    *,
    raw: bool = False,
    until: date | None = None,
) -> OaiBatch:
    """Return only complete harvests; `since` and `until` (both inclusive) address metadata
    modification dates, and `ALL` harvests every category.

    Reuse the checkpoint's UTC date inclusively on the next harvest. This overlaps
    the first response day, avoiding gaps from records modified during pagination.
    """
    original = {
        "verb": "ListRecords",
        "metadataPrefix": "arXivRaw" if raw else "arXiv",
        "from": since.isoformat(),
    }
    if until is not None:
        original["until"] = until.isoformat()
    if category != ALL:
        if not re.fullmatch(r"[a-z-]+\.[A-Za-z]+", category):
            raise SourceError(f"Unsupported OAI category: {category}")
        archive, subject = category.split(".", 1)
        original["set"] = f"{archive}:{archive}:{subject}"
    params = original.copy()
    papers: dict[str, Paper] = {}
    checkpoint = None
    seen_tokens: set[str] = set()
    restarted = False
    deadline = time.monotonic() + 15 * 60
    for _ in range(200):
        if time.monotonic() > deadline:
            raise OverBudget("OAI exceeded its 15-minute harvest budget")
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
            paper = _raw_paper(record) if raw else _paper(record)
            if paper is not None:
                papers[paper.id] = paper
        token = listing.findtext("o:resumptionToken", "", NS).strip()
        if not token:
            return OaiBatch(list(papers.values()), checkpoint)
        if token in seen_tokens:
            raise SourceError("OAI repeated a resumption token")
        seen_tokens.add(token)
        params = {"verb": "ListRecords", "resumptionToken": token}
    raise OverBudget("OAI exceeded the 200-page harvest bound")


def _raw_paper(record: ET.Element) -> Paper | None:
    """Read explicit revision history; raw author credit is never used to split names.

    Schema: https://arxiv.org/OAI/arXivRaw.xsd. OAI header datestamps are metadata
    modification dates, not revision numbers or submission timestamps.
    """
    header = record.find("o:header", NS)
    if header is None:
        raise SourceError("OAI raw record missing header")
    if header.get("status") == "deleted":
        return None
    node = record.find("o:metadata/r:arXivRaw", NS)
    if node is None:
        raise SourceError("OAI record missing arXivRaw metadata")
    paper_id = _required(node, "r:id")
    if not _ID.fullmatch(paper_id):
        raise SourceError(f"Invalid arXiv raw ID: {paper_id}")
    history = []
    for entry in node.findall("r:version", NS):
        match = re.fullmatch(r"v([1-9]\d*)", entry.get("version", ""))
        if not match:
            raise SourceError(f"Invalid arXiv version for {paper_id}")
        value = _required(entry, "r:date")
        try:
            submitted = parsedate_to_datetime(value)
            if submitted.tzinfo is None:
                raise ValueError("timezone required")
        except (TypeError, ValueError) as exc:
            raise SourceError(f"Invalid arXiv version date: {value}") from exc
        history.append(
            {
                "version": int(match[1]),
                "date": submitted.astimezone(UTC).isoformat(),
                "size": entry.findtext("r:size", None, NS),
                "source_type": entry.findtext("r:source_type", None, NS),
            }
        )
    if not history or len({v["version"] for v in history}) != len(history):
        raise SourceError(f"Missing or duplicate arXiv versions for {paper_id}")
    history.sort(key=lambda v: v["version"])
    return Paper(
        id=re.sub(r"v\d+$", "", paper_id),
        title=node.findtext("r:title", "", NS),
        abstract=node.findtext("r:abstract", "", NS),
        version=history[-1]["version"],
        version_history=history,
    )


def resolve_observed_version(
    paper: Paper, history: dict, source: str, observed_at: str | None = None
) -> Paper:
    """Choose the latest explicit revision present at the metadata observation.

    OAI canonical timestamps have day precision. Metadata-only edits after a
    revision do not create a new revision; later raw history must not make an old
    outbox observation claim a future revision either.
    """
    observed = paper.updated or paper.published or observed_at
    if not observed:
        raise SourceError("revision observation date unavailable")
    boundary = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    if boundary.tzinfo is None:
        boundary = boundary.replace(tzinfo=UTC)
    day_precision = (source == "oai" and bool(paper.updated or paper.published)) or len(
        observed
    ) == 10
    if day_precision:
        boundary = boundary.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    eligible = []
    for entry in history.get("version_history", []):
        submitted = datetime.fromisoformat(entry["date"].replace("Z", "+00:00"))
        if submitted.tzinfo is None:
            submitted = submitted.replace(tzinfo=UTC)
        if submitted < boundary if day_precision else submitted <= boundary:
            eligible.append(entry)
    if not eligible:
        raise SourceError("no explicit revision at the metadata observation")
    version = max(entry["version"] for entry in eligible)
    if type(version) is not int or version < 1:
        raise SourceError("invalid resolved revision")
    return paper.model_copy(update={"version": version, "version_history": eligible})


def fetch_versions(paper_id: str, client: XmlClient) -> dict:
    """Resolve an unknown revision without guessing from a metadata update timestamp."""
    if not _ID.fullmatch(paper_id):
        raise SourceError(f"Invalid arXiv ID: {paper_id}")
    try:
        root = ET.fromstring(
            client.get(
                URL,
                params={
                    "verb": "GetRecord",
                    "metadataPrefix": "arXivRaw",
                    "identifier": f"oai:arXiv.org:{paper_id}",
                },
            )
        )
    except ET.ParseError as exc:
        raise SourceError("Malformed OAI raw XML") from exc
    record = root.find("o:GetRecord/o:record", NS)
    if record is None:
        raise SourceError(f"OAI revision record unavailable for {paper_id}")
    paper = _raw_paper(record)
    if paper is None or paper.id != re.sub(r"v\d+$", "", paper_id):
        raise SourceError(f"OAI revision record deleted or mismatched for {paper_id}")
    return {"version": paper.version, "version_history": paper.version_history}


def fetch_category(
    category: str,
    since: date,
    client: XmlClient,
    *,
    include_versions: bool = False,
    until: date | None = None,
) -> OaiBatch:
    """Canonical author/date metadata plus optional raw revision history, of one category or
    of `ALL`, changed from `since` to `until` (open when None).

    A raw endpoint failure leaves actionable unknown-version intents. It must not
    prevent successful canonical intake or erase its durable category coverage.
    """
    batch = _harvest(category, since, client, until=until)
    if not include_versions:
        return batch
    try:
        raw = _harvest(category, since, client, raw=True, until=until)
        histories = {p.id: p for p in raw.papers}
        missing, unresolved = [], []
        for paper in batch.papers:
            if paper.id not in histories:
                missing.append(paper.id)
                continue
            history = histories[paper.id]
            # One unresolvable record keeps an unknown version; it must not stop the rest.
            try:
                resolved = resolve_observed_version(
                    paper,
                    {"version": history.version, "version_history": history.version_history},
                    "oai",
                )
            except SourceError:
                unresolved.append(paper.id)
                continue
            paper.version = resolved.version
            paper.version_history = resolved.version_history
            # Canonical <created> can be a later revision's date; v1 is the first posting.
            if resolved.version_history[0]["version"] == 1:
                paper.published = resolved.version_history[0]["date"]
        if missing or unresolved:
            batch.versions_error = (
                f"Raw history absent for {len(missing)} and unresolved for "
                f"{len(unresolved)} canonical records"
            )
    except SourceError as exc:
        batch.versions_error = str(exc)
    return batch
