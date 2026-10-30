"""Intake's catalog pass against disposable PostgreSQL, with arXiv and the archive faked."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from litwatch import intake
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core import Paper
from litwatch.intake import atom, catalog, oai
from litwatch.intake.http import SourceError, XmlClient
from litwatch.integrations import IntegrationError
from litwatch.integrations import archive as archive_integration
from litwatch.store import runs
from tests.fakes_http import FakeArchive, mock

NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
TODAY = NOW.date()
SINCE = NOW - timedelta(days=3)


def paper(id: str, categories: list[str], version: int | None = 1) -> Paper:
    return Paper(
        id=id,
        title=f"Title {id}",
        abstract="Abstract",
        authors=["Alice Smith"],
        categories=categories,
        published=NOW.isoformat(),
        url=f"https://arxiv.org/abs/{id}",
        version=version,
    )


class Arxiv:
    """OAI-PMH as intake sees it: the category passes find `listed`, and the catalog pass the
    records of `days` (a modification day's records) in its window. A window wider than
    `budget` days is over budget; one holding a day of `unresolved` has a versions error."""

    def __init__(self, days=None, listed=(), budget=None, unresolved=()):
        self.days: dict[date, list[Paper]] = days or {}
        self.listed = list(listed)
        self.budget = budget
        self.unresolved = set(unresolved)
        self.windows: list[tuple[date, date | None]] = []

    def fetch_category(self, category, since, client, *, include_versions=False, until=None):
        if category != oai.ALL:
            return oai.OaiBatch(self.listed, NOW)
        assert include_versions
        self.windows.append((since, until))
        last = until or TODAY
        if self.budget and (last - since).days + 1 > self.budget:
            raise oai.OverBudget("OAI exceeded the 200-page harvest bound")
        found = [p for day, ps in self.days.items() if since <= day <= last for p in ps]
        batch = oai.OaiBatch(found, NOW)
        if any(since <= day <= last for day in self.unresolved):
            batch.versions_error = "Raw history absent for 1 and unresolved for 0 canonical records"
        return batch


def unavailable(*args, **kwargs):
    raise SourceError("upstream unavailable")


def archive(fake: FakeArchive) -> ArchiveClient:
    return ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))


async def checkpoint(conn) -> date | None:
    row = await (
        await conn.execute("SELECT checkpoint FROM intake_checkpoints WHERE category='*'")
    ).fetchone()
    return row and row["checkpoint"]


async def collect(conn, fake: FakeArchive, categories=("cs.CL",)):
    run_id = await runs.start(conn, "intake", SINCE, NOW)
    return await intake.collect(conn, categories, SINCE, run_id, archive=archive(fake))


async def walk(conn, arxiv: Arxiv, fake: FakeArchive, since: date):
    run_id = await runs.start(conn, "intake", SINCE, NOW)
    with XmlClient() as client:
        return await catalog.run(
            conn, archive(fake), ("cs.CL",), since, run_id, client, source=arxiv
        )


async def test_registers_the_rest_of_arxiv_as_metadata_only(conn, monkeypatch):
    ours = paper("2610.00001", ["cs.CL"])
    vision = paper("2610.00002", ["cs.CV"], version=2)
    cross = paper("2610.00003", ["cs.CV", "cs.CL"])
    arxiv = Arxiv(days={TODAY - timedelta(days=1): [vision, cross]}, listed=[ours])
    monkeypatch.setattr(atom, "fetch_category", unavailable)
    monkeypatch.setattr(oai, "fetch_category", arxiv.fetch_category)
    fake = FakeArchive()

    result = await collect(conn, fake)

    # Only the paper outside intake's categories, with its version, and no file asked for.
    assert set(fake.records) == {vision.id}
    assert fake.records[vision.id]["latest_version"] == 2
    assert fake.records[vision.id]["metadata"]["categories"] == ["cs.CV"]
    assert fake.jobs == []
    # The collection holds only what the category pass read.
    stored = await (await conn.execute("SELECT id FROM papers")).fetchall()
    assert [r["id"] for r in stored] == [ours.id]
    assert result.details["status"] == "complete"
    assert result.details["catalog"] | {"through": None} == {
        "from": SINCE.date().isoformat(),
        "complete": True,
        "windows": 1,
        "records": 2,
        "registered": 1,
        "through": None,
    }
    assert await checkpoint(conn) == TODAY
    # The next night goes on from that day, overlapping it.
    await collect(conn, fake)
    assert arxiv.windows[-1] == (TODAY, None)


async def test_a_backfill_walks_weeks_and_moves_the_checkpoint_after_each(conn):
    start = TODAY - timedelta(days=16)
    days = {start + timedelta(days=i): [paper(f"2609.{i:05d}", ["cs.CV"])] for i in range(17)}
    arxiv = Arxiv(days=days)
    fake = FakeArchive()
    await catalog.lower(conn, start)

    done = await walk(conn, arxiv, fake, TODAY)

    assert arxiv.windows == [
        (start, start + timedelta(days=6)),
        (start + timedelta(days=7), start + timedelta(days=13)),
        (start + timedelta(days=14), None),
    ]
    assert done["complete"] and done["registered"] == 17 == len(fake.records)
    assert await checkpoint(conn) == TODAY


async def test_a_failed_registration_keeps_its_window_open(conn):
    start = TODAY - timedelta(days=10)
    days = {start + timedelta(days=i): [paper(f"2609.{i:05d}", ["cs.CV"])] for i in range(11)}
    fake = FakeArchive()
    handle = fake.handle

    def refuse_the_second_week(req):
        if req.method == "PUT" and req.url.path.endswith("2609.00008"):
            return httpx.Response(503)
        return handle(req)

    fake.handle = refuse_the_second_week
    await catalog.lower(conn, start)

    done = await walk(conn, Arxiv(days=days), fake, TODAY)

    assert not done["complete"] and "503" in done["error"]
    assert done["through"] == (start + timedelta(days=6)).isoformat()
    assert await checkpoint(conn) == start + timedelta(days=6)
    # The same command resumes from there; a run from an earlier day starts over.
    await catalog.lower(conn, start)
    assert await checkpoint(conn) == start + timedelta(days=6)
    await catalog.lower(conn, start - timedelta(days=1))
    assert await checkpoint(conn) is None


async def test_a_window_over_budget_is_halved(conn):
    start = TODAY - timedelta(days=13)
    arxiv = Arxiv(budget=3)
    await catalog.lower(conn, start)

    done = await walk(conn, arxiv, FakeArchive(), TODAY)

    assert done["complete"]
    # 7 days are over budget, 3 fit, and the pass stays at 3 days a window.
    assert arxiv.windows[:3] == [
        (start, start + timedelta(days=6)),
        (start, start + timedelta(days=2)),
        (start + timedelta(days=3), start + timedelta(days=5)),
    ]
    assert all(((u or TODAY) - s).days < 3 for s, u in arxiv.windows[1:])


async def test_a_day_with_unresolved_versions_stops_the_pass_before_it(conn):
    start = TODAY - timedelta(days=6)
    bad = start + timedelta(days=4)
    days = {start + timedelta(days=i): [paper(f"2609.{i:05d}", ["cs.CV"])] for i in range(7)}
    fake = FakeArchive()
    await catalog.lower(conn, start)

    done = await walk(conn, Arxiv(days=days, unresolved=[bad]), fake, TODAY)

    assert not done["complete"] and "versions" in done["error"]
    assert await checkpoint(conn) == bad - timedelta(days=1)
    # What came before that day is registered, and nothing from it or after.
    assert sorted(fake.records) == [f"2609.{i:05d}" for i in range(4)]


@pytest.mark.parametrize("categories", [("cs.CL",), ("cs.AI", "cs.CL")])
async def test_a_failed_pass_leaves_intake_as_its_categories_made_it(conn, monkeypatch, categories):
    def harvest(category, *args, **kwargs):
        if category == oai.ALL:
            raise SourceError("upstream unavailable")
        if category == "cs.AI":
            raise SourceError("unavailable")
        return oai.OaiBatch([paper("2610.00001", ["cs.CL"])], NOW)

    monkeypatch.setattr(atom, "fetch_category", unavailable)
    monkeypatch.setattr(oai, "fetch_category", harvest)

    result = await collect(conn, FakeArchive(), categories)

    assert result.details["status"] == ("partial" if "cs.AI" in categories else "complete")
    assert result.inserted == 1
    assert result.details["catalog"]["error"] == "SourceError: upstream unavailable"
    assert await checkpoint(conn) is None


@pytest.mark.parametrize(
    ("section", "on"),
    [
        ({"harvest": True}, True),
        ({}, False),
        ({"harvest": True, "register": False}, False),
        ({"harvest": True, "enabled": False}, False),
    ],
)
def test_the_pass_runs_only_with_harvest_on_and_registering(section, on, monkeypatch):
    monkeypatch.setenv("LITWATCH_ARCHIVE_URL", "http://archive.test")
    monkeypatch.setenv("LITWATCH_ARCHIVE_TOKEN", "archive-token")
    config = Config(integrations={"archive": section})
    assert (archive_integration.harvester(config) is not None) is on


def test_harvest_must_be_true_or_false(monkeypatch):
    monkeypatch.setenv("LITWATCH_ARCHIVE_TOKEN", "archive-token")
    with pytest.raises(IntegrationError, match="harvest"):
        archive_integration.load("archive", {"url": "http://a", "harvest": "yes"}, Config())
