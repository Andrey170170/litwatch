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


def raw_record(paper_id="2609.12345", version="v2", version_date="Sat, 12 Sep 2026 12:00:00 GMT"):
    return f'''<record><header/><metadata><arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
    <id>{paper_id}</id><submitter>Someone</submitter>
    <version version="v1"><date>Thu, 10 Sep 2026 10:00:00 GMT</date></version>
    <version version="{version}"><date>{version_date}</date><source_type>tex</source_type></version>
    <title>A paper</title><authors>Alice Smith</authors><categories>cs.CL</categories>
    </arXivRaw></metadata></record>'''


def test_raw_enrichment_retains_canonical_authors_and_explicit_history():
    client = Client([page(record()), page(raw_record())])
    batch = fetch_category("cs.CL", date(2026, 9, 12), client, include_versions=True)
    paper = batch.papers[0]
    assert paper.version == 2
    assert paper.authors == ["Alice Smith"]
    assert paper.published == "2026-09-10T10:00:00+00:00"
    assert paper.updated == "2026-09-12T00:00:00+00:00"
    assert paper.version_history[-1]["date"] == "2026-09-12T12:00:00+00:00"
    assert client.calls[1]["metadataPrefix"] == "arXivRaw"


def test_published_is_the_first_version_even_when_created_names_a_revision():
    # arXiv reported <created> as v2's date for a paper first posted months earlier.
    revised = record().replace("<created>2026-09-10", "<created>2026-09-12")
    raw = raw_record().replace("Thu, 10 Sep 2026", "Wed, 27 May 2026")
    batch = fetch_category(
        "cs.CL", date(2026, 9, 12), Client([page(revised), page(raw)]), include_versions=True
    )
    assert batch.papers[0].published == "2026-05-27T10:00:00+00:00"


@pytest.mark.parametrize(
    "raw", [raw_record(version="v0"), raw_record(version_date="bad"), raw_record(version="v1"), ""]
)
def test_invalid_raw_history_leaves_canonical_intake_and_unknown_version(raw):
    batch = fetch_category(
        "cs.CL", date(2026, 9, 12), Client([page(record()), page(raw)]), include_versions=True
    )
    assert batch.papers[0].version is None
    assert batch.versions_error


def test_one_unresolvable_history_keeps_resolving_the_rest_of_the_batch():
    # Every raw revision of the first paper postdates its canonical observation day.
    future = raw_record("2609.11111").replace("Thu, 10 Sep", "Mon, 14 Sep")
    future = future.replace("Sat, 12 Sep", "Mon, 14 Sep")
    client = Client([page(record("2609.11111") + record()), page(future + raw_record())])
    batch = fetch_category("cs.CL", date(2026, 9, 12), client, include_versions=True)
    assert [p.version for p in batch.papers] == [None, 2]
    assert batch.versions_error == "Raw history absent for 0 and unresolved for 1 canonical records"


def test_get_record_version_resolution_checks_identity():
    from litwatch.oai_source import fetch_versions

    raw = page(raw_record()).replace(b"ListRecords", b"GetRecord")
    assert fetch_versions("2609.12345", Client([raw]))["version"] == 2
    with pytest.raises(SourceError, match="mismatched"):
        fetch_versions("2609.22222", Client([raw]))


def test_raw_history_newer_than_canonical_snapshot_does_not_relabel_old_metadata():
    raw = raw_record(version_date="Sun, 13 Sep 2026 12:00:00 GMT")
    batch = fetch_category(
        "cs.CL", date(2026, 9, 12), Client([page(record()), page(raw)]), include_versions=True
    )
    assert batch.papers[0].version == 1
    assert [v["version"] for v in batch.papers[0].version_history] == [1]


def test_versionless_feed_resolves_using_observation_not_submission_timestamp():
    from litwatch.models import Paper
    from litwatch.oai_source import resolve_observed_version

    paper = Paper(id="2609.12345", title="T", abstract="A")
    resolved = resolve_observed_version(
        paper,
        {
            "version_history": [
                {"version": 1, "date": "2026-09-10T12:00:00Z"},
                {"version": 2, "date": "2026-09-12T12:00:00Z"},
            ]
        },
        "atom",
        "2026-09-11T12:00:00Z",
    )
    assert resolved.version == 1
    assert resolved.published is None and resolved.updated is None
