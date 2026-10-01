"""Real PostgreSQL durability and intake boundaries, in a disposable schema."""

import os
from datetime import timedelta
from uuid import uuid4

import pytest

from litwatch import archive_outbox, atom_source, db, intake, oai_source
from litwatch.archive_outbox import ArchivePolicy
from tests.test_atom_source import entry
from tests.test_intake import NOW, SINCE, canonical, snapshot, unavailable

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="needs test PostgreSQL")
POLICY = ArchivePolicy(categories=("cs.CL", "cs.AI", "cs.LG"))


@pytest.fixture
async def conn():
    async with await db.connect(os.environ["TEST_DATABASE_URL"]) as c:
        schema = "test_archive_" + uuid4().hex
        await c.execute(f'CREATE SCHEMA "{schema}"')
        await c.execute(f'SET search_path TO "{schema}"')
        await db.apply_schema(c)
        try:
            yield c
        finally:
            await c.execute("SET search_path TO public")
            await c.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def entries(conn):
    return await (await conn.execute("SELECT * FROM archive_outbox ORDER BY id")).fetchall()


async def test_every_tier_and_revision_queued_without_resetting_screen(conn):
    p = canonical().model_copy(update={"version": 1})
    await db.upsert_papers(conn, [p], archive_policy=POLICY)
    await conn.execute("UPDATE papers SET screened_at=now(),best_tier=4")
    before = await (await conn.execute("SELECT screened_at FROM papers")).fetchone()
    await db.upsert_papers(conn, [p], archive_policy=POLICY)
    assert len(await entries(conn)) == 3
    doi = p.model_copy(update={"doi": "10.1234/example"})
    await db.upsert_papers(conn, [doi], archive_policy=POLICY)
    assert len(await entries(conn)) == 4  # metadata edit, same PDF/source identities
    revision = doi.model_copy(update={"version": 2})
    await db.upsert_papers(conn, [revision], archive_policy=POLICY)
    assert len(await entries(conn)) == 7
    stored = await (await conn.execute("SELECT * FROM papers")).fetchone()
    assert stored["screened_at"] == before["screened_at"] and stored["best_tier"] == 4
    assert db.row_to_paper(stored).version == 2


async def test_unknown_versions_are_actionable_and_out_of_scope_is_excluded(conn):
    await db.upsert_papers(conn, [canonical()], archive_policy=POLICY)
    await db.upsert_papers(
        conn,
        [canonical("2609.22222").model_copy(update={"categories": ["math.AG"]})],
        archive_policy=POLICY,
    )
    rows = await entries(conn)
    assert len(rows) == 3
    assert all(row["version"] is None and row["status"] == "pending" for row in rows)
    summary = await archive_outbox.status(conn)
    assert summary["unresolved_versions"] == 2


async def test_claim_leases_and_acceptance_distinct_from_availability(conn):
    await db.upsert_papers(
        conn, [canonical().model_copy(update={"version": 1})], archive_policy=POLICY
    )
    await archive_outbox.promote(conn, "2609.11934")
    (first,) = await archive_outbox.claim(conn, limit=1)
    assert first["kind"] == "pdf" and first["priority"] == 0
    others = await archive_outbox.claim(conn)
    assert first["id"] not in {r["id"] for r in others}
    assert not await archive_outbox.complete(conn, first["id"], "wrong", status="available")
    assert await archive_outbox.complete(
        conn,
        first["id"],
        first["lease_token"],
        status="delivered",
        remote_job_id=42,
        retry_seconds=0,
    )
    (poll,) = await archive_outbox.claim(conn, limit=1)
    assert poll["status"] == "delivered" and poll["remote_job_id"] == "42"
    assert await archive_outbox.complete(conn, poll["id"], poll["lease_token"], status="available")
    assert not await archive_outbox.complete(
        conn, first["id"], first["lease_token"], status="error"
    )


async def test_expired_worker_cannot_ack_reclaimed_job(conn):
    await db.upsert_papers(conn, [canonical()], archive_policy=POLICY)
    (first,) = await archive_outbox.claim(conn, limit=1)
    await conn.execute(
        "UPDATE archive_outbox SET lease_until=now()-interval '1 second' WHERE id=%s",
        (first["id"],),
    )
    (second,) = await archive_outbox.claim(conn, limit=1)
    assert second["id"] == first["id"] and second["lease_token"] != first["lease_token"]
    assert not await archive_outbox.complete(
        conn, first["id"], first["lease_token"], status="available"
    )


async def test_checkpoint_rejection_rolls_back_intents_and_metadata(conn, monkeypatch):
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)
    monkeypatch.setattr(
        oai_source, "fetch_category", lambda *a, **kw: oai_source.OaiBatch([canonical()], NOW)
    )
    await conn.execute(
        "ALTER TABLE intake_checkpoints ADD CONSTRAINT fail CHECK(checkpoint IS NULL)"
    )
    run = await db.start_run(conn, "daily", SINCE, NOW)
    with pytest.raises(Exception, match="fail"):
        await intake.collect(conn, ("cs.CL",), SINCE, run, archive_policy=POLICY)
    assert await entries(conn) == []
    assert await (await conn.execute("SELECT * FROM papers")).fetchall() == []
    (coverage,) = await (await conn.execute("SELECT * FROM archive_intake_checkpoints")).fetchall()
    assert coverage["checkpoint"] is None


async def test_replacement_only_fallback_archives_without_curated_candidate(conn, monkeypatch):
    monkeypatch.setattr(
        atom_source, "fetch_category", lambda *a: snapshot(entry("2501.12345v3", "replace"))
    )
    monkeypatch.setattr(oai_source, "fetch_category", unavailable)
    run = await db.start_run(conn, "daily", SINCE, NOW)
    result = await intake.collect(conn, ("cs.CL",), SINCE, run, archive_policy=POLICY)
    assert result.inserted == 0
    assert await (await conn.execute("SELECT * FROM papers")).fetchall() == []
    rows = await entries(conn)
    assert {r["kind"] for r in rows} == {"metadata", "pdf", "source"}
    assert all(r["paper_id"] == "2501.12345" and r["version"] == 3 for r in rows)


async def test_raw_failure_persists_unknown_intents_and_canonical_coverage(conn, monkeypatch):
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)
    monkeypatch.setattr(
        oai_source,
        "fetch_category",
        lambda *a, **kw: oai_source.OaiBatch([canonical()], NOW, "raw unavailable"),
    )
    run = await db.start_run(conn, "daily", SINCE, NOW)
    result = await intake.collect(conn, ("cs.CL",), SINCE, run, archive_policy=POLICY)
    assert result.details["status"] == "complete"
    assert result.details["categories"]["cs.CL"]["archive_versions"]["status"] == "partial"
    assert len(await entries(conn)) == 3
    (coverage,) = await (await conn.execute("SELECT * FROM archive_intake_checkpoints")).fetchall()
    assert coverage["checkpoint"] == NOW.date()
    assert coverage["discovery_from"] == SINCE.date()


async def test_archival_checkpoint_replays_gap_after_disabled_period(conn, monkeypatch):
    monkeypatch.setattr(atom_source, "fetch_category", unavailable)
    starts = []

    def harvest(category, since, client, **kwargs):
        starts.append(since)
        return oai_source.OaiBatch([], NOW)

    monkeypatch.setattr(oai_source, "fetch_category", harvest)
    old = (SINCE - timedelta(days=8)).date()
    await conn.execute(
        "INSERT INTO archive_intake_checkpoints(category,policy,discovery_from) "
        "VALUES('cs.CL',%s,%s)",
        (POLICY.name, old),
    )
    run = await db.start_run(conn, "daily", SINCE, NOW)
    await intake.collect(conn, ("cs.CL",), SINCE, run, archive_policy=POLICY)
    assert starts == [old]


async def test_newer_unknown_canonical_metadata_clears_stale_revision(conn):
    original = canonical().model_copy(
        update={"version": 1, "version_history": [{"version": 1, "date": SINCE.isoformat()}]}
    )
    await db.upsert_papers(conn, [original], source="oai", archive_policy=POLICY)
    same_day = original.model_copy(update={"version": None, "version_history": []})
    await db.upsert_papers(conn, [same_day], source="oai", archive_policy=POLICY)
    stored = await (await conn.execute("SELECT * FROM papers")).fetchone()
    assert stored["version"] == 1
    newer = same_day.model_copy(update={"updated": (NOW + timedelta(days=1)).isoformat()})
    await db.upsert_papers(conn, [newer], source="oai", archive_policy=POLICY)
    stored = await (await conn.execute("SELECT * FROM papers")).fetchone()
    assert stored["version"] is None and stored["version_history"] == []
    unknown = [r for r in await entries(conn) if r["version"] is None and r["kind"] == "pdf"]
    assert len(unknown) == 2


async def test_versionless_rss_preserves_snapshot_time_as_provenance(conn, monkeypatch):
    monkeypatch.setattr(atom_source, "fetch_category", lambda *a: snapshot(entry("2609.11934")))
    monkeypatch.setattr(oai_source, "fetch_category", unavailable)
    run = await db.start_run(conn, "daily", SINCE, NOW)
    await intake.collect(conn, ("cs.CL",), SINCE, run, archive_policy=POLICY)
    rows = await entries(conn)
    assert all(row["payload"]["observed_at"] == NOW.isoformat() for row in rows)
    assert all(row["payload"]["paper"]["published"] is None for row in rows)
