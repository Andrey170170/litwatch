from datetime import UTC, date, datetime

import pytest

from litwatch.oai_source import fetch_category
from litwatch.source_http import SourceError


def record(paper_id="2609.12345", extra=""):
    return f"""<record><header/><metadata><arXiv xmlns="http://arxiv.org/OAI/arXiv/">
    <id>{paper_id}</id><created>2026-09-10</created><updated>2026-09-12</updated>
    <authors><author><keyname>Smith</keyname><forenames>Alice</forenames></author></authors>
    <title>A paper</title><abstract>Some research.</abstract><categories>cs.CL cs.AI</categories>
    {extra}</arXiv></metadata></record>"""


def page(content, token="", error=None):
    body = (
        f'<error code="{error}">problem</error>'
        if error
        else f"<ListRecords>{content}<resumptionToken>{token}</resumptionToken></ListRecords>"
    )
    return f"""<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
    <responseDate>2026-09-14T12:00:00Z</responseDate>{body}</OAI-PMH>""".encode()


class Client:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(params.copy())
        return next(self.pages)


def test_complete_paging_and_canonical_metadata():
    client = Client([page(record(), "next"), page(record("hep-th/9901001v2"))])
    batch = fetch_category("cs.CL", date(2026, 9, 12), client)
    assert batch.checkpoint == datetime(2026, 9, 14, 12, tzinfo=UTC)
    assert [p.id for p in batch.papers] == ["2609.12345", "hep-th/9901001"]
    assert batch.papers[0].published == "2026-09-10T00:00:00+00:00"
    assert batch.papers[0].authors == ["Alice Smith"]
    assert batch.papers[0].primary_category is None
    assert client.calls == [
        {"verb": "ListRecords", "metadataPrefix": "arXiv", "from": "2026-09-12", "set": "cs:cs:CL"},
        {"verb": "ListRecords", "resumptionToken": "next"},
    ]


def test_expired_token_restarts_once_and_deduplicates():
    client = Client(
        [
            page(record(), "x"),
            page("", error="badResumptionToken"),
            page(record(), "y"),
            page(record("2609.12346")),
        ]
    )
    assert len(fetch_category("cs.AI", date(2026, 9, 12), client).papers) == 2
    assert client.calls[0] == client.calls[2]


@pytest.mark.parametrize(
    "pages",
    [
        [page(record(), "x"), page(record(), "x")],
        [
            page(record(), "x"),
            page("", error="badResumptionToken"),
            page(record(), "y"),
            page("", error="badResumptionToken"),
        ],
        [page("", error="badArgument")],
        [page("")],
        [page(record().replace("<abstract>Some research.</abstract>", ""))],
        [page(record().replace("2026-09-10", "bad"))],
        [b"<wrong/>"],
        [b"not xml"],
    ],
)
def test_incomplete_harvest_never_returns_success(pages):
    with pytest.raises(SourceError):
        fetch_category("cs.CL", date(2026, 9, 12), Client(pages))


def test_explicit_empty_and_deletions():
    assert (
        fetch_category(
            "cs.LG", date(2026, 9, 12), Client([page("", error="noRecordsMatch")])
        ).papers
        == []
    )
    assert (
        fetch_category(
            "cs.LG",
            date(2026, 9, 12),
            Client([page('<record><header status="deleted"/></record>')]),
        ).papers
        == []
    )
