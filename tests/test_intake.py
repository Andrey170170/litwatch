"""Intake recovery against disposable PostgreSQL; network and model calls are stubbed."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from litwatch import atom_source, db, ingest, intake, oai_source
from litwatch.config import Settings
from litwatch.models import Paper
from litwatch.source_http import SourceError
from tests.conftest import screen_result
from tests.test_atom_source import entry, feed
from tests.test_digest_history import FAITH_D, Writer

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="needs test PostgreSQL")
NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
SINCE = NOW - timedelta(days=3)


@pytest.fixture
async def conn():
    async with await db.connect(os.environ["TEST_DATABASE_URL"]) as c:
        schema = "test_intake_" + uuid4().hex
        await c.execute(f'CREATE SCHEMA "{schema}"')
        await c.execute(f'SET search_path TO "{schema}"')
        await db.apply_schema(c)
        try:
            yield c
        finally:
            await c.execute("SET search_path TO public")
            await c.execute(f'DROP SCHEMA "{schema}" CASCADE')


def unavailable(*args, **kwargs):
    raise SourceError("upstream unavailable")


def snapshot(*entries):
    raw = feed("".join(entries))
    raw = raw.replace(b"2026-09-14T20:00:00Z", NOW.isoformat().encode())
    raw = raw.replace(b"2026-09-14T00:00:00Z", NOW.isoformat().encode())
    return atom_source.parse_feed(raw)


def canonical(id="2609.11934"):
    return Paper(
        id=id,
        title="Canonical title",
        abstract="Canonical abstract",
        authors=["Alice Smith", "Bob Jones"],
        categories=["cs.CL"],
        published=(NOW - timedelta(days=1)).isoformat(),
        updated=NOW.isoformat(),
        primary_category="cs.CL",
        url=f"https://arxiv.org/abs/{id}",
    )


async def collect(conn, categories=("cs.CL",)):
    run_id = await db.start_run(conn, "daily", SINCE, NOW)
    return run_id, await intake.collect(conn, categories, SINCE, run_id)


async def rows(conn, table):
    return await (await conn.execute(f"SELECT * FROM {table}")).fetchall()


async def test_fallback_archive_replay_then_canonical_reconciliation(conn, monkeypatch):
    atom = snapshot(entry(), entry("2609.11935v2", "replace"))
    monkeypatch.setattr(atom_source, "fetch_category", lambda *args: atom)
    monkeypatch.setattr(oai_source, "fetch_category", unavailable)
    run_id, result = await collect(conn)
    assert result.inserted == 1
    assert result.details["status"] == "partial"
    assert (await rows(conn, "runs"))[0]["intake"] == result.details
    (stored,) = await rows(conn, "papers")
    assert stored["id"] == "2609.11934"  # revision-only records are not new discovery
    assert stored["published"] is None and stored["updated"] is None
    assert stored["metadata_source"] == "atom"
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] is None
    assert len(await rows(conn, "intake_snapshots")) == 1

    # Simulate recovery from an archived feed when its earlier candidate insert was lost.
    await conn.execute("DELETE FROM papers")
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)
    _, replay = await collect(conn)
    assert replay.inserted == 1
    assert replay.details["categories"]["cs.CL"]["source"] == "atom-cache"
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] is None

    paper = canonical()
    monkeypatch.setattr(
        oai_source, "fetch_category", lambda *args, **kwargs: oai_source.OaiBatch([paper], NOW)
    )
    _, recovered = await collect(conn)
    assert recovered.details["status"] == "complete"
    assert recovered.inserted == 0 and recovered.updated == 1
    (stored,) = await rows(conn, "papers")
    assert stored["authors"] == paper.authors
    assert stored["published"] == datetime.fromisoformat(paper.published)
    assert stored["metadata_source"] == "oai"
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] == NOW.date()
    # A later outage and lower-fidelity snapshot must not overwrite canonical metadata.
    monkeypatch.setattr(oai_source, "fetch_category", unavailable)
    monkeypatch.setattr(atom_source, "fetch_category", lambda *args: atom)
    await collect(conn)
    assert (await rows(conn, "papers"))[0] == stored


async def test_category_failure_does_not_discard_successful_category(conn, monkeypatch):
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)

    def harvest(category, *args, **kwargs):
        if category == "cs.AI":
            raise SourceError("unavailable")
        return oai_source.OaiBatch([canonical()], NOW)

    monkeypatch.setattr(oai_source, "fetch_category", harvest)
    _, result = await collect(conn, ("cs.AI", "cs.CL"))
    assert result.details["status"] == "partial"
    assert result.details["categories"]["cs.CL"]["complete"]
    assert not result.details["categories"]["cs.AI"]["complete"]
    assert len(await rows(conn, "papers")) == 1
    checkpoints = {r["category"]: r["checkpoint"] for r in await rows(conn, "intake_checkpoints")}
    assert checkpoints == {"cs.AI": None, "cs.CL": NOW.date()}


async def test_checkpoint_failure_rolls_back_category_papers_but_keeps_archive(conn, monkeypatch):
    monkeypatch.setattr(atom_source, "fetch_category", lambda *args: snapshot(entry()))
    monkeypatch.setattr(
        oai_source,
        "fetch_category",
        lambda *args, **kwargs: oai_source.OaiBatch([canonical()], NOW),
    )
    await conn.execute(
        "ALTER TABLE intake_checkpoints ADD CONSTRAINT reject_advance CHECK (checkpoint IS NULL)"
    )
    with pytest.raises(Exception, match="reject_advance"):
        await collect(conn)
    assert await rows(conn, "papers") == []
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] is None
    assert len(await rows(conn, "intake_snapshots")) == 1


async def test_paper_failure_rolls_back_other_papers_and_checkpoint(conn, monkeypatch):
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)
    monkeypatch.setattr(
        oai_source,
        "fetch_category",
        lambda *args, **kwargs: oai_source.OaiBatch([canonical(), canonical("2609.11935")], NOW),
    )
    await conn.execute("ALTER TABLE papers ADD CONSTRAINT reject_paper CHECK (id <> '2609.11935')")
    with pytest.raises(Exception, match="reject_paper"):
        await collect(conn)
    assert await rows(conn, "papers") == []
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] is None


async def test_all_sources_fail_pending_paper_still_screened_and_digest_warns(conn, monkeypatch):
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)
    monkeypatch.setattr(oai_source, "fetch_category", unavailable)
    await db.upsert_selection(conn, FAITH_D)
    await db.upsert_papers(conn, [canonical()])
    cascade = SimpleNamespace(
        selections=[FAITH_D],
        screen=AsyncMock(return_value=screen_result("2609.11934", cot_faithfulness=1)),
    )
    writer = Writer()
    result = await ingest.run(
        conn,
        Settings(openrouter_api_key="test", categories=("cs.CL",), intake_source="oai"),
        cascade,
        "daily",
        3,
        digester=writer,
    )
    assert result.error is None
    assert result.screened == 1 and result.failed == 0
    assert result.intake["status"] == "partial"
    cascade.screen.assert_awaited_once()
    assert writer.seen == {"general": ["2609.11934"], FAITH_D.slug: ["2609.11934"]}
    saved = await db.list_digests(conn)
    assert len(saved) == 2
    assert all("Incomplete paper intake" in d["body"] for d in saved)
    assert all("cs.CL" in d["body"] for d in saved)
    (run,) = await rows(conn, "runs")
    assert run["finished_at"] is not None and run["error"] is None
    assert run["intake"]["status"] == "partial"


async def test_old_unknown_changed_paper_is_admitted_without_daily_announcement(conn, monkeypatch):
    # An old paper newly cross-listed today is discoverable only via OAI after Atom rotates.
    paper = canonical("2001.00001").model_copy(update={"published": "2020-01-01T00:00:00+00:00"})
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)
    monkeypatch.setattr(
        oai_source, "fetch_category", lambda *args, **kwargs: oai_source.OaiBatch([paper], NOW)
    )
    _, result = await collect(conn)
    assert result.details["status"] == "complete"
    assert result.inserted == 1
    (stored,) = await rows(conn, "papers")
    assert stored["id"] == paper.id
    assert stored["published"] == datetime(2020, 1, 1, tzinfo=UTC)
    assert stored["screened_at"] is None


async def test_older_oai_metadata_cannot_downgrade_existing_paper(conn, monkeypatch):
    paper = canonical()
    await db.upsert_papers(conn, [paper])
    (before,) = await rows(conn, "papers")
    stale = paper.model_copy(
        update={
            "title": "Stale title",
            "authors": ["Stale author"],
            "updated": (NOW - timedelta(days=2)).isoformat(),
        }
    )
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)
    monkeypatch.setattr(
        oai_source, "fetch_category", lambda *args, **kwargs: oai_source.OaiBatch([stale], NOW)
    )
    _, result = await collect(conn)
    assert result.details["status"] == "complete"
    assert result.inserted == 0 and result.updated == 0
    assert (await rows(conn, "papers"))[0] == before
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] == NOW.date()


async def test_revision_keeps_the_first_posting_and_the_verdict_unless_rewritten(conn):
    paper = canonical().model_copy(update={"version": 1})
    await db.upsert_papers(conn, [paper], source="oai")
    await conn.execute("UPDATE papers SET screened_at = now()")
    first = (await rows(conn, "papers"))[0]["published"]
    revised = paper.model_copy(
        update={"version": 2, "published": NOW.isoformat(), "abstract": "Canonical abstract."}
    )
    await db.upsert_papers(conn, [revised], source="oai")
    (stored,) = await rows(conn, "papers")
    assert stored["published"] == first and stored["version"] == 2
    assert stored["screened_at"] is not None
    # Canonical text that the verdict never saw is screened again.
    await conn.execute("UPDATE papers SET best_tier = 1, zotero_key = 'K'")
    rewrite = revised.model_copy(update={"version": 3, "abstract": "Something else entirely."})
    await db.upsert_papers(conn, [rewrite], source="oai")
    assert (await rows(conn, "papers"))[0]["screened_at"] is None
    # Routing and deep processing wait for the new screen instead of the stale verdict.
    assert await db.routable(conn) == [] and await db.deep_pending(conn) == []


async def test_shown_metadata_changes_reach_the_projection_and_others_do_not(conn):
    paper = canonical().model_copy(update={"version": 1})
    await db.upsert_papers(conn, [paper], source="oai")
    await conn.execute("UPDATE papers SET screened_at = now() - interval '3 days', best_tier = 2")
    since = NOW - timedelta(days=1)

    async def projected():
        return [p["id"] for p in await db.list_papers(conn, None, [2], since, 10)]

    await db.upsert_papers(conn, [paper.model_copy(update={"doi": "10.1/x"})], source="oai")
    assert (await rows(conn, "papers"))[0]["metadata_changed_at"] is None
    assert await projected() == []
    earlier = (NOW - timedelta(days=90)).isoformat()
    await db.upsert_papers(conn, [paper.model_copy(update={"published": earlier})], source="oai")
    assert (await rows(conn, "papers"))[0]["metadata_changed_at"] is not None
    assert await projected() == [paper.id]
    # Under the limit, the most recent change wins over the most recent screening.
    await db.upsert_papers(conn, [canonical("2609.11935")], source="oai")
    await conn.execute(
        "UPDATE papers SET screened_at = now() - interval '2 hours', best_tier = 2, "
        "metadata_changed_at = now() - interval '1 hour' WHERE id = '2609.11935'"
    )
    await conn.execute("UPDATE papers SET metadata_changed_at = now() WHERE id = %s", (paper.id,))
    assert [p["id"] for p in await db.list_papers(conn, None, [2], since, 1)] == [paper.id]


async def test_screened_atom_fallback_is_rescreened_when_canonical_text_differs(conn, monkeypatch):
    calls = []

    def harvest(*args, **kwargs):
        calls.append(kwargs)
        return oai_source.OaiBatch([canonical()], NOW)

    fallback = canonical().model_copy(update={"abstract": "A short feed abstract."})
    await db.upsert_papers(conn, [fallback], source="atom")
    await conn.execute("UPDATE papers SET screened_at = now()")
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)
    monkeypatch.setattr(oai_source, "fetch_category", harvest)
    await collect(conn)
    (stored,) = await rows(conn, "papers")
    assert stored["metadata_source"] == "oai" and stored["screened_at"] is None
    # Without archive delivery, version history is still harvested for the digest.
    assert calls == [{"include_versions": True}]


async def test_no_fetch_run_retains_previous_gap_in_stored_digest(conn, monkeypatch):
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)
    monkeypatch.setattr(oai_source, "fetch_category", unavailable)
    prior_id, partial = await collect(conn)
    await db.finish_run(conn, prior_id, error=None, intake=partial.details)
    await db.upsert_selection(conn, FAITH_D)
    await db.upsert_papers(conn, [canonical()])
    cascade = SimpleNamespace(
        selections=[FAITH_D],
        screen=AsyncMock(return_value=screen_result("2609.11934", cot_faithfulness=1)),
    )

    # These would be programmer errors, not swallowed SourceError, if fetch=False was ignored.
    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("no-fetch run contacted a source")

    monkeypatch.setattr(atom_source, "fetch_category", unexpected_fetch)
    monkeypatch.setattr(oai_source, "fetch_category", unexpected_fetch)
    result = await ingest.run(
        conn,
        Settings(openrouter_api_key="test", categories=("cs.CL",), intake_source="oai"),
        cascade,
        "daily",
        3,
        fetch=False,
        digester=Writer(),
    )
    assert result.error is None and result.screened == 1
    assert result.intake["status"] == "partial"
    assert result.intake["categories"] == partial.details["categories"]
    digests = await db.list_digests(conn)
    assert len(digests) == 2
    assert all("Incomplete paper intake" in d["body"] for d in digests)
    assert all(SINCE.date().isoformat() in d["body"] for d in digests)
    run = await (
        await conn.execute("SELECT intake FROM runs WHERE id=%s", (result.run_id,))
    ).fetchone()
    assert run["intake"]["status"] == "partial"
