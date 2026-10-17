"""Intake from the archive: the client's window listing, the source that stores what it lists,
and the job's run. The archive is an in-memory fake; the database tests need PostgreSQL."""

import asyncio
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from litwatch import events
from litwatch.archive import ArchiveClient, ArchiveError
from litwatch.config import Config, Intake
from litwatch.core import Event
from litwatch.intake import archive as archive_intake
from litwatch.intake.collect import _state
from litwatch.integrations.archive import Archive
from litwatch.jobs import intake as intake_job
from litwatch.store import papers, runs
from tests.conftest import paper
from tests.fakes_http import FakeArchive, mock

SINCE, UNTIL = date(2026, 10, 1), date(2026, 10, 14)


def item(n: int, **fields) -> dict:
    """A `/v1/papers` item."""
    return {
        "id": f"2610.{n:05d}",
        "version": 1,
        "title": f"Paper {n}",
        "abstract": f"Abstract {n}.",
        "authors": ["Alice Smith"],
        "categories": ["cs.CL"],
        "primary_category": None,
        "published": "2026-10-02",
        "updated": "2026-10-02",
        "version_history": [{"version": 1, "date": "2026-10-02T10:00:00+00:00"}],
        "doi": None,
        "journal_ref": None,
        "license": None,
    } | fields


def client(fake: FakeArchive) -> ArchiveClient:
    return ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))


async def listed(fake: FakeArchive, **kwargs) -> list[list[str]]:
    return [[p.id for p in page] async for page in client(fake).papers(SINCE, UNTIL, **kwargs)]


async def test_the_client_follows_the_cursor_to_the_end_of_the_window():
    fake = FakeArchive()
    fake.listing = [item(n) for n in range(1, 6)]
    pages = await listed(fake, categories=["cs.CL", "cs.AI"], limit=2)
    assert pages == [["2610.00001", "2610.00002"], ["2610.00003", "2610.00004"], ["2610.00005"]]
    assert fake.listings[0] == {
        "since": "2026-10-01",
        "until": "2026-10-14",
        "categories": "cs.CL,cs.AI",
        "limit": "2",
    }
    assert [p.get("cursor") for p in fake.listings] == [None, "2", "4"]

    (first,), *_ = [page async for page in client(fake).papers(SINCE, UNTIL, limit=1)]
    assert first.published == first.updated == "2026-10-02T00:00:00+00:00"  # as OAI's days
    assert first.url == "https://arxiv.org/abs/2610.00001" and first.version == 1
    assert "categories" not in fake.listings[-1]


async def test_a_malformed_listing_is_an_archive_error():
    for body in ({"items": "nope", "next": None}, {"items": [{"title": "no id"}], "next": None}):
        fake = client(FakeArchive())
        fake.http = mock(lambda req, body=body: httpx.Response(200, json=body))
        with pytest.raises(ArchiveError):
            [page async for page in fake.papers(SINCE, UNTIL)]


async def outbox(conn) -> list[tuple[str, str, dict]]:
    rows = await (await conn.execute("SELECT * FROM outbox ORDER BY id")).fetchall()
    return [(r["type"], r["paper_id"], r["payload"]) for r in rows]


async def test_the_source_stores_new_papers_and_applies_revisions(conn):
    fake = FakeArchive()
    fake.listing = [item(1), item(2, abstract=None)]
    result = await archive_intake.collect(conn, client(fake), ["cs.CL"], SINCE, UNTIL)
    assert (result.fetched, result.inserted, result.details["skipped"]) == (1, 1, ["2610.00002"])
    stored = await papers.get(conn, "2610.00001")
    assert stored.published == "2026-10-02T00:00:00+00:00" and stored.version == 1
    row = await (await conn.execute("SELECT metadata_source FROM papers")).fetchone()
    assert row["metadata_source"] == "archive"

    fake.listing = [item(1, version=2, abstract="Rewritten entirely.", updated="2026-10-09")]
    revised = await archive_intake.collect(conn, client(fake), ["cs.CL"], SINCE, UNTIL)
    assert (revised.updated, revised.rewritten) == (1, ["2610.00001"])
    again = await archive_intake.collect(conn, client(fake), ["cs.CL"], SINCE, UNTIL)
    assert (again.inserted, again.updated, again.rewritten) == (0, 0, [])
    assert await outbox(conn) == [
        ("paper.added", "2610.00001", {"version": 1, "source": "archive"}),
        (
            "paper.revised",
            "2610.00001",
            {"version": 2, "previous_version": 1, "rewritten": True, "source": "archive"},
        ),
    ]


async def test_the_archive_never_rolls_back_what_arxiv_sent(conn):
    at = datetime(2026, 10, 9, 15, 30, tzinfo=UTC).isoformat()
    oai = paper("2610.00001", version=2, abstract="Current.", published=at, updated=at)
    await papers.upsert(conn, [oai], source="oai")
    fake = FakeArchive()
    unproven = [
        item(1, abstract="Stale.", updated="2026-10-02"),  # an older version on an older day
        item(1, version=None, abstract="Unknown.", updated="2026-10-09"),  # no version, same day
        item(1, version=2, abstract="Same version.", updated="2026-10-09"),  # nothing newer
    ]
    for listed in unproven:
        fake.listing = [listed]
        result = await archive_intake.collect(conn, client(fake), ["cs.CL"], SINCE, UNTIL)
        assert result.updated == 0, listed
    stored = await papers.get(conn, oai.id)
    assert (stored.abstract, stored.version, stored.updated) == ("Current.", 2, at)

    fake.listing = [item(1, version=2, title=oai.title, abstract="Fixed.", updated="2026-10-10")]
    later = await archive_intake.collect(conn, client(fake), ["cs.CL"], SINCE, UNTIL)
    assert later.updated == 1
    stored = await papers.get(conn, oai.id)
    assert (stored.abstract, stored.updated) == ("Fixed.", "2026-10-10T00:00:00+00:00")


async def test_the_job_records_its_run_and_arxiv_intake_does_not_go_on_from_it(conn):
    fake = FakeArchive()
    fake.listing = [item(1), item(2)]
    config = Config(intake=Intake(categories=("cs.CL", "cs.AI")))
    old = (date(2026, 1, 1), date(2026, 1, 14))
    result = await intake_job.from_archive(conn, config, client(fake), *old)
    assert result.inserted == 2
    assert fake.listings[0]["categories"] == "cs.CL,cs.AI"
    run = await (await conn.execute("SELECT * FROM runs")).fetchone()
    assert run["kind"] == "intake" and run["finished_at"] is not None
    assert (run["window_from"], run["window_to"]) == (
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 15, tzinfo=UTC),
    )
    assert run["intake"]["source"] == "archive" and run["fetched"] == 2

    # The next arXiv harvest starts from its own window, not the archive read's.
    since = datetime.now(UTC).date() - timedelta(days=3)
    run_id = await runs.start(conn, "intake")
    assert (await _state(conn, "cs.CL", since, run_id))["discovery_from"] == since


class Stalling:
    """An archive client that lists a page, then waits on the next one until cancelled."""

    def __init__(self, page: list) -> None:
        self.page, self.listed = page, asyncio.Event()

    async def papers(self, since, until, categories):
        yield self.page
        self.listed.set()
        await asyncio.Event().wait()


async def test_a_cancelled_intake_records_its_run_as_finished(conn):
    archive = Stalling([paper("2610.00001")])
    config = Config(intake=Intake(categories=("cs.CL",)))
    job = asyncio.create_task(intake_job.from_archive(conn, config, archive, SINCE, UNTIL))
    await archive.listed.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    run = await (await conn.execute("SELECT * FROM runs")).fetchone()
    assert run["finished_at"] is not None and run["error"].startswith("CancelledError")
    assert await papers.get(conn, "2610.00001")  # the page read before it stays stored


def test_the_archive_integration_skips_papers_read_from_it():
    archive = Archive(client(FakeArchive()))
    for kind in (events.PAPER_ADDED, events.PAPER_REVISED):
        assert archive.wants(Event(type=kind, paper_id="2610.00001", payload={"source": "oai"}))
    added = Event(type=events.PAPER_ADDED, paper_id="2610.00001", payload={"source": "archive"})
    assert not archive.wants(added)
    # A revision read from it is not registered again, only checked against the storage
    # policy (test_integrations.py).
