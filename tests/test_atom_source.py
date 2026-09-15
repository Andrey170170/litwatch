from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest

from litwatch.atom_source import fetch_category, parse_feed
from litwatch.source_http import SourceError


def entry(paper_id="2609.11934v1", announcement="new", abstract="An abstract."):
    return f"""<entry>
      <id>oai:arXiv.org:{paper_id}</id><title>A paper</title>
      <summary>arXiv:{paper_id} Announce Type: {announcement}
Abstract: {abstract}</summary>
      <arxiv:announce_type>{announcement}</arxiv:announce_type>
      <published>2026-09-14T00:00:00Z</published>
      <updated>2026-09-14T20:00:00Z</updated>
      <dc:creator>Smith, Jr., Alice, Bob Jones</dc:creator>
      <category term="cs.CL"/><category term="cs.AI"/>
    </entry>"""


def feed(entries=""):
    return f"""<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
      <updated>2026-09-14T20:00:00Z</updated>{entries}</feed>""".encode()


def test_metadata_preserves_credit_but_not_announcement_as_submission_date():
    raw = feed(entry(abstract="An abstract. Abstract: a second label is retained."))
    batch = parse_feed(raw)
    (paper,) = batch.papers
    assert paper.id == "2609.11934"
    assert paper.abstract == "An abstract. Abstract: a second label is retained."
    assert paper.authors == ["Smith, Jr., Alice, Bob Jones"]
    assert paper.categories == ["cs.CL", "cs.AI"]
    assert paper.primary_category is None
    assert paper.published is None and paper.updated is None
    assert batch.announcement_dates == [date(2026, 9, 14)]
    assert batch.generated_at == datetime(2026, 9, 14, 20, tzinfo=UTC)
    assert batch.raw == raw


@pytest.mark.parametrize("announcement", ["new", "cross", "replace", "replace-cross"])
def test_announcements_and_old_ids(announcement):
    batch = parse_feed(feed(entry("hep-th/9901001v2", announcement)))
    assert batch.papers[0].id == "hep-th/9901001"
    assert batch.announcements == {"hep-th/9901001": announcement}


def test_empty_feed_has_generation_time_but_no_coverage():
    batch = parse_feed(feed())
    assert batch.papers == []
    assert batch.announcement_dates == []
    assert batch.announcements == {}


@pytest.mark.parametrize(
    "raw",
    [b"<", b"<feed/>", b"<html/>", feed().replace(b"2026-09-14T20:00:00Z", b"2026-09-14T20:00:00")],
)
def test_malformed_feeds(raw):
    with pytest.raises(SourceError):
        parse_feed(raw)


@pytest.mark.parametrize(
    "xml",
    [
        entry(announcement="unknown"),
        entry(abstract=""),
        entry(paper_id="not-an-id"),
        entry().replace("<title>A paper</title>", ""),
        entry().replace('term="cs.CL"', 'term=""'),
        entry().replace("Abstract:", "Description:"),
        entry().replace("Announce Type: new", "Announce Type: cross"),
        entry().replace("arXiv:2609.11934v1 Announce", "arXiv:2609.11935v1 Announce"),
    ],
)
def test_missing_or_inconsistent_metadata_fails(xml):
    with pytest.raises(SourceError):
        parse_feed(feed(xml))


def test_limit_is_not_silently_complete():
    with pytest.raises(SourceError, match="truncation"):
        parse_feed(feed(entry() * 2000))


def test_duplicates_are_deduplicated_but_conflicts_rejected():
    assert len(parse_feed(feed(entry() * 2)).papers) == 1
    with pytest.raises(SourceError, match="Conflicting"):
        parse_feed(feed(entry() + entry(abstract="Different abstract.")))


def test_fetch_uses_shared_transport():
    client = Mock()
    client.get.return_value = feed(entry())
    assert fetch_category("cs.CL", client).papers[0].id == "2609.11934"
    client.get.assert_called_once_with("https://rss.arxiv.org/atom/cs.CL")
