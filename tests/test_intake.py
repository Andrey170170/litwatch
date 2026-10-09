"""Intake recovery against disposable PostgreSQL; the network is stubbed."""

from datetime import UTC, datetime, timedelta

import pytest

from litwatch import intake
from litwatch.core import Paper
from litwatch.intake import atom, oai
from litwatch.intake.http import SourceError
from litwatch.store import papers, runs
from tests.test_intake_atom import entry, feed

NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
SINCE = NOW - timedelta(days=3)


def unavailable(*args, **kwargs):
    raise SourceError("upstream unavailable")


def snapshot(*entries):
    raw = feed("".join(entries))
    raw = raw.replace(b"2026-09-14T20:00:00Z", NOW.isoformat().encode())
    raw = raw.replace(b"2026-09-14T00:00:00Z", NOW.isoformat().encode())
    return atom.parse_feed(raw)


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
    run_id = await runs.start(conn, "intake", SINCE, NOW)
    return run_id, await intake.collect(conn, categories, SINCE, run_id)


async def rows(conn, table):
    return await (await conn.execute(f"SELECT * FROM {table}")).fetchall()


async def test_fallback_archive_replay_then_canonical_reconciliation(conn, monkeypatch):
    announced = snapshot(entry(), entry("2609.11935v2", "replace"))
    monkeypatch.setattr(atom, "fetch_category", lambda *args: announced)
    monkeypatch.setattr(oai, "fetch_category", unavailable)
    run_id, result = await collect(conn)
    assert result.inserted == 1
    assert result.details["status"] == "partial"
    (stored,) = await rows(conn, "papers")
    assert stored["id"] == "2609.11934"  # revision-only records are not new discovery
    assert stored["published"] is None and stored["updated"] is None
    assert stored["metadata_source"] == "atom"
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] is None
    assert len(await rows(conn, "intake_snapshots")) == 1

    # Simulate recovery from an archived feed when its earlier candidate insert was lost.
    await conn.execute("DELETE FROM papers")
    monkeypatch.setattr(atom, "fetch_category", unavailable)
    _, replay = await collect(conn)
    assert replay.inserted == 1
    assert replay.details["categories"]["cs.CL"]["source"] == "atom-cache"
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] is None

    paper = canonical()
    monkeypatch.setattr(oai, "fetch_category", lambda *args, **kwargs: oai.OaiBatch([paper], NOW))
    _, recovered = await collect(conn)
    assert recovered.details["status"] == "complete"
    assert recovered.inserted == 0 and recovered.updated == 1
    (stored,) = await rows(conn, "papers")
    assert stored["authors"] == paper.authors
    assert stored["published"] == datetime.fromisoformat(paper.published)
    assert stored["metadata_source"] == "oai"
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] == NOW.date()
    # A later outage and lower-fidelity snapshot must not overwrite canonical metadata.
    monkeypatch.setattr(oai, "fetch_category", unavailable)
    monkeypatch.setattr(atom, "fetch_category", lambda *args: announced)
    await collect(conn)
    assert (await rows(conn, "papers"))[0] == stored


async def test_category_failure_does_not_discard_successful_category(conn, monkeypatch):
    monkeypatch.setattr(atom, "fetch_category", unavailable)

    def harvest(category, *args, **kwargs):
        if category == "cs.AI":
            raise SourceError("unavailable")
        return oai.OaiBatch([canonical()], NOW)

    monkeypatch.setattr(oai, "fetch_category", harvest)
    _, result = await collect(conn, ("cs.AI", "cs.CL"))
    assert result.details["status"] == "partial"
    assert result.details["categories"]["cs.CL"]["complete"]
    assert not result.details["categories"]["cs.AI"]["complete"]
    assert len(await rows(conn, "papers")) == 1
    checkpoints = {r["category"]: r["checkpoint"] for r in await rows(conn, "intake_checkpoints")}
    assert checkpoints == {"cs.AI": None, "cs.CL": NOW.date()}


async def test_checkpoint_failure_rolls_back_category_papers_but_keeps_archive(conn, monkeypatch):
    monkeypatch.setattr(atom, "fetch_category", lambda *args: snapshot(entry()))
    monkeypatch.setattr(
        oai,
        "fetch_category",
        lambda *args, **kwargs: oai.OaiBatch([canonical()], NOW),
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
    monkeypatch.setattr(atom, "fetch_category", unavailable)
    monkeypatch.setattr(
        oai,
        "fetch_category",
        lambda *args, **kwargs: oai.OaiBatch([canonical(), canonical("2609.11935")], NOW),
    )
    await conn.execute("ALTER TABLE papers ADD CONSTRAINT reject_paper CHECK (id <> '2609.11935')")
    with pytest.raises(Exception, match="reject_paper"):
        await collect(conn)
    assert await rows(conn, "papers") == []
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] is None


async def test_old_unknown_changed_paper_is_admitted_without_daily_announcement(conn, monkeypatch):
    # An old paper newly cross-listed today is discoverable only via OAI after Atom rotates.
    paper = canonical("2001.00001").model_copy(update={"published": "2020-01-01T00:00:00+00:00"})
    monkeypatch.setattr(atom, "fetch_category", unavailable)
    monkeypatch.setattr(oai, "fetch_category", lambda *args, **kwargs: oai.OaiBatch([paper], NOW))
    _, result = await collect(conn)
    assert result.details["status"] == "complete"
    assert result.inserted == 1
    (stored,) = await rows(conn, "papers")
    assert stored["id"] == paper.id
    assert stored["published"] == datetime(2020, 1, 1, tzinfo=UTC)


async def test_older_oai_metadata_cannot_downgrade_existing_paper(conn, monkeypatch):
    paper = canonical()
    await papers.upsert(conn, [paper])
    (before,) = await rows(conn, "papers")
    stale = paper.model_copy(
        update={
            "title": "Stale title",
            "authors": ["Stale author"],
            "updated": (NOW - timedelta(days=2)).isoformat(),
        }
    )
    monkeypatch.setattr(atom, "fetch_category", unavailable)
    monkeypatch.setattr(oai, "fetch_category", lambda *args, **kwargs: oai.OaiBatch([stale], NOW))
    _, result = await collect(conn)
    assert result.details["status"] == "complete"
    assert result.inserted == 0 and result.updated == 0
    assert (await rows(conn, "papers"))[0] == before
    assert (await rows(conn, "intake_checkpoints"))[0]["checkpoint"] == NOW.date()


async def events(conn):
    found = await rows(conn, "outbox ORDER BY id")
    return [(e["type"], e["paper_id"], e["payload"]) for e in found]


async def test_revision_keeps_the_first_posting_and_is_rewritten_only_when_the_text_changed(conn):
    paper = canonical().model_copy(update={"version": 1})
    await papers.upsert(conn, [paper], source="oai")
    first = (await rows(conn, "papers"))[0]["published"]
    revised = paper.model_copy(
        update={"version": 2, "published": NOW.isoformat(), "abstract": "Canonical abstract."}
    )
    touched = await papers.upsert(conn, [revised], source="oai")
    (stored,) = await rows(conn, "papers")
    assert stored["published"] == first and stored["version"] == 2
    assert touched.updated == 1 and touched.rewritten == []
    rewrite = revised.model_copy(update={"version": 3, "abstract": "Something else entirely."})
    assert (await papers.upsert(conn, [rewrite], source="oai")).rewritten == [paper.id]
    # A refresh that changes neither the version nor the text is not a revision.
    await papers.upsert(conn, [rewrite.model_copy(update={"doi": "10.1/x"})], source="oai")
    assert await events(conn) == [
        ("paper.added", paper.id, {"version": 1}),
        ("paper.revised", paper.id, {"version": 2, "previous_version": 1, "rewritten": False}),
        ("paper.revised", paper.id, {"version": 3, "previous_version": 2, "rewritten": True}),
    ]


async def test_canonical_text_replacing_a_feed_abstract_counts_as_rewritten(conn, monkeypatch):
    calls = []

    def harvest(*args, **kwargs):
        calls.append(kwargs)
        return oai.OaiBatch([canonical()], NOW)

    fallback = canonical().model_copy(update={"abstract": "A short feed abstract."})
    await papers.upsert(conn, [fallback], source="atom")
    monkeypatch.setattr(atom, "fetch_category", unavailable)
    monkeypatch.setattr(oai, "fetch_category", harvest)
    _, result = await collect(conn)
    (stored,) = await rows(conn, "papers")
    assert stored["metadata_source"] == "oai" and result.rewritten == [stored["id"]]
    assert calls == [{"include_versions": True}]
