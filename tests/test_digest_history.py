"""Real PostgreSQL regression tests; TEST_DATABASE_URL must point to a disposable DB."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from litwatch import db
from litwatch.digest import Digest
from litwatch.digest_history import write_run_digests
from tests.conftest import FAITH, INTERP, screen_result

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="needs test PostgreSQL")
FAITH_D = FAITH.model_copy(update={"digest": True})
INTERP_D = INTERP.model_copy(update={"digest": True})


@pytest.fixture
async def conn():
    async with await db.connect(os.environ["TEST_DATABASE_URL"]) as c:
        schema = "test_digest_" + uuid4().hex
        await c.execute(f'CREATE SCHEMA "{schema}"')
        await c.execute(f'SET search_path TO "{schema}"')
        await db.apply_schema(c)
        try:
            yield c
        finally:
            await c.execute("SET search_path TO public")
            await c.execute(f'DROP SCHEMA "{schema}" CASCADE')


class Writer:
    def __init__(self, fail=()):
        self.fail = fail
        self.seen = {}

    async def write_general(self, selections, groups):
        return self.result(
            "general", [p for tiers in groups.values() for ps in tiers.values() for p in ps]
        )

    async def write_selection(self, selection, groups):
        return self.result(selection.slug, [p for ps in groups.values() for p in ps])

    def result(self, source, papers):
        self.seen[source] = [p["id"] for p in papers]
        if source in self.fail:
            raise RuntimeError("generation failed")
        return Digest(source, source, " | ".join(self.seen[source]) or "empty")


async def run(c):
    now = datetime.now(UTC)
    return await db.start_run(c, "daily", now, now)


async def paper(c, id, tier=1, selection=FAITH_D):
    await db.upsert_selection(c, selection)
    await c.execute(
        "INSERT INTO papers (id,title,abstract,published,best_tier,screened_at) "
        "VALUES (%s,%s,'abstract','2020-01-01',%s,now()) "
        "ON CONFLICT (id) DO UPDATE SET screened_at=now(),best_tier=EXCLUDED.best_tier",
        (id, id, tier),
    )
    await c.execute(
        "INSERT INTO verdicts (paper_id,selection,tier,confidence,reason,version) "
        "VALUES (%s,%s,%s,'high','reason','v1') ON CONFLICT (paper_id,selection) "
        "DO UPDATE SET tier=EXCLUDED.tier,screened_at=now()",
        (id, selection.slug, tier),
    )


async def test_rescreen_and_promotion_do_not_repeat_but_old_publication_is_new(conn):
    first = await run(conn)
    await paper(conn, "old", tier=2)
    await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
    await db.finish_run(conn, first, error=None)
    second = await run(conn)
    await paper(conn, "old", tier=1)
    await paper(conn, "new")
    writer = Writer()
    await write_run_digests(conn, writer, [FAITH_D], second, store=True)
    assert writer.seen == {"general": ["new"], FAITH_D.slug: ["new"]}
    digests = await db.list_digests(conn)
    assert all(
        d["digest_tiers"] == {"1": 1, "2": 0, "3": 0} for d in digests if d["run_id"] == second
    )


async def test_failure_retries_per_source_and_preview_does_not_consume(conn):
    first = await run(conn)
    await paper(conn, "p")
    await write_run_digests(conn, Writer(fail=[FAITH_D.slug]), [FAITH_D], first, store=True)
    await db.finish_run(conn, first, error=None)
    second = await run(conn)
    preview = Writer()
    await write_run_digests(conn, preview, [FAITH_D], second, store=False)
    assert preview.seen == {"general": [], FAITH_D.slug: ["p"]}
    actual = Writer()
    await write_run_digests(conn, actual, [FAITH_D], second, store=True)
    assert actual.seen == preview.seen
    # Retrying the older failed issue must not steal coverage from the newer issue.
    retry = Writer()
    await write_run_digests(conn, retry, [FAITH_D], first, store=True)
    assert retry.seen.get(FAITH_D.slug, []) == []


async def test_regeneration_uses_saved_members_and_verdicts(conn):
    first = await run(conn)
    await paper(conn, "p")
    await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
    await db.finish_run(conn, first, error=None)
    second = await run(conn)
    await paper(conn, "p", tier=4)
    await paper(conn, "later")
    await write_run_digests(conn, Writer(), [FAITH_D], second, store=True)
    writer = Writer()
    await write_run_digests(conn, writer, [], first, store=True)
    assert writer.seen == {"general": ["p"], FAITH_D.slug: ["p"]}


async def test_sources_are_independent_and_counted_members_are_covered(conn):
    first = await run(conn)
    await paper(conn, "p", tier=3)
    await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
    await db.finish_run(conn, first, error=None)
    second = await run(conn)
    await paper(conn, "p", selection=INTERP_D)
    writer = Writer()
    await write_run_digests(conn, writer, [FAITH_D, INTERP_D], second, store=True)
    assert writer.seen == {"general": [], INTERP_D.slug: ["p"]}


async def test_history_migration_handles_versions_sources_and_is_idempotent(conn):
    from litwatch.digest_history import seed_history

    first = await run(conn)
    await paper(conn, "2601.00001")
    await db.store_digest(
        conn, first, FAITH_D.slug, "CoT", "[paper](http://arxiv.org/abs/2601.00001v2)", None, None
    )
    await conn.execute(
        "UPDATE runs SET digest='[paper](https://arxiv.org/abs/2601.00001v1)' WHERE id=%s", (first,)
    )
    await conn.execute("DELETE FROM digest_history_migrations")
    await seed_history(conn)
    await seed_history(conn)
    rows = await (
        await conn.execute("SELECT source,paper_id,run_id FROM digest_coverage ORDER BY source")
    ).fetchall()
    assert rows == [
        {"source": FAITH_D.slug, "paper_id": "2601.00001", "run_id": first},
        {"source": "general", "paper_id": "2601.00001", "run_id": first},
    ]


async def test_digest_and_coverage_save_atomically(conn):
    first = await run(conn)
    await paper(conn, "p")
    # Reject ledger inserts, simulating a DB failure after the body has been written.
    await conn.execute("ALTER TABLE digest_coverage ADD CONSTRAINT reject CHECK (paper_id <> 'p')")
    with pytest.raises(Exception, match="reject"):
        await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
    assert not await (await conn.execute("SELECT * FROM digests")).fetchall()
    assert not await (await conn.execute("SELECT * FROM digest_coverage")).fetchall()


async def test_run_failure_before_generation_is_not_lost(conn):
    first = await run(conn)
    await paper(conn, "old")
    await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
    await db.finish_run(conn, first, error=None)
    failed = await run(conn)
    await paper(conn, "delayed")
    await db.finish_run(conn, failed, error="deep service unavailable")
    next_run = await run(conn)
    writer = Writer()
    await write_run_digests(conn, writer, [FAITH_D], next_run, store=True)
    assert writer.seen == {"general": ["delayed"], FAITH_D.slug: ["delayed"]}


async def test_competing_generation_is_rejected_and_lock_is_released(conn):
    from litwatch.digest_history import DIGEST_LOCK

    first = await run(conn)
    async with await db.connect(os.environ["TEST_DATABASE_URL"]) as other:
        await other.execute("SELECT pg_advisory_lock(%s)", (DIGEST_LOCK,))
        with pytest.raises(RuntimeError, match="in progress"):
            await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
        await other.execute("SELECT pg_advisory_unlock(%s)", (DIGEST_LOCK,))
        with pytest.raises(ValueError, match="unknown run"):
            await write_run_digests(conn, Writer(), [FAITH_D], 9999, store=True)
        row = await (
            await other.execute("SELECT pg_try_advisory_lock(%s) AS locked", (DIGEST_LOCK,))
        ).fetchone()
        assert row["locked"]


async def test_rewriting_historical_issue_cannot_advance_analysis_cutoff(conn):
    old_run = await run(conn)
    await paper(conn, "old")
    await db.store_digest(conn, old_run, "general", "General", "old", None, None)
    await db.finish_run(conn, old_run, error=None)
    next_run = await run(conn)
    await paper(conn, "today")
    # No historical snapshot exists yet, as on the first rewrite after migration.
    await write_run_digests(conn, Writer(), [FAITH_D], old_run, store=True)
    writer = Writer()
    await write_run_digests(conn, writer, [FAITH_D], next_run, store=True)
    assert writer.seen == {"general": ["today"], FAITH_D.slug: ["today"]}


async def test_legacy_prose_restores_only_explicit_topic_section_coverage(conn):
    from litwatch.digest_history import seed_history

    first = await run(conn)
    await paper(conn, "faith")
    await paper(conn, "other", selection=INTERP_D)
    titles = {
        "faith": "A Complete Benchmark for Faithful Reasoning",
        "other": "A Complete Benchmark for Sparse Autoencoders",
    }
    for id, title in titles.items():
        await conn.execute("UPDATE papers SET title=%s WHERE id=%s", (title, id))
    await conn.execute(
        "UPDATE runs SET digest=%s WHERE id=%s",
        (
            f"{FAITH_D.name}: {titles['faith']} presents new results.\\n\\n".replace("\\n", "\n")
            + f"{INTERP_D.name}: {titles['other']} presents other results.",
            first,
        ),
    )
    await conn.execute("DELETE FROM digest_history_migrations")
    await seed_history(conn)
    rows = await (
        await conn.execute("SELECT source,paper_id FROM digest_coverage ORDER BY source,paper_id")
    ).fetchall()
    assert rows == [
        {"source": FAITH_D.slug, "paper_id": "faith"},
        {"source": "general", "paper_id": "faith"},
        {"source": "general", "paper_id": "other"},
        {"source": INTERP_D.slug, "paper_id": "other"},
    ]


async def test_batch_preparation_is_atomic(conn):
    first = await run(conn)
    await paper(conn, "p")
    await conn.execute("ALTER TABLE digest_batches ADD CONSTRAINT reject CHECK (source='general')")
    with pytest.raises(Exception, match="reject"):
        await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
    assert not await (await conn.execute("SELECT * FROM digest_batches")).fetchall()


async def test_new_issue_refreshes_failed_candidates_after_demotion(conn):
    first = await run(conn)
    await paper(conn, "p")
    await write_run_digests(conn, Writer(fail=[FAITH_D.slug]), [FAITH_D], first, store=True)
    await db.finish_run(conn, first, error=None)
    second = await run(conn)
    await paper(conn, "p", tier=4)
    writer = Writer()
    await write_run_digests(conn, writer, [FAITH_D], second, store=True)
    assert writer.seen == {"general": []}


async def test_concurrent_screening_waits_and_gets_timestamp_after_digest(conn):
    from litwatch.digest_history import DIGEST_LOCK

    await paper(conn, "p")
    schema = (await (await conn.execute("SELECT current_schema() AS name")).fetchone())["name"]
    await conn.execute("SELECT pg_advisory_lock(%s)", (DIGEST_LOCK,))
    async with await db.connect(os.environ["TEST_DATABASE_URL"]) as other:
        await other.execute(f'SET search_path TO "{schema}"')
        task = asyncio.create_task(db.store_screen(other, screen_result("p", cot_faithfulness=1)))
        try:
            # Wait for proof the screening transaction is blocked on the digest's lock.
            async with asyncio.timeout(3):
                while True:
                    row = await (
                        await conn.execute(
                            "SELECT 1 FROM pg_locks WHERE pid=%s "
                            "AND locktype='advisory' AND NOT granted",
                            (other.info.backend_pid,),
                        )
                    ).fetchone()
                    if row:
                        break
                    await asyncio.sleep(0.01)
            cutoff = datetime.now(UTC)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (DIGEST_LOCK,))
            await asyncio.wait_for(task, 3)
        row = await (await conn.execute("SELECT screened_at FROM papers WHERE id='p'")).fetchone()
        assert row["screened_at"] > cutoff


async def test_older_paper_is_marked_in_the_frozen_snapshot(conn):
    first = await run(conn)
    await paper(conn, "late")
    await paper(conn, "fresh")
    await conn.execute("UPDATE papers SET published = now() WHERE id = 'fresh'")
    await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
    (snapshot,) = await (
        await conn.execute("SELECT snapshot FROM digest_batches WHERE source='general'")
    ).fetchall()
    marks = {p["id"]: p["first_posted"] for p in snapshot["snapshot"]["papers"]}
    assert marks["fresh"] is None and marks["late"].startswith("2020-01-01")


async def test_a_new_version_of_a_covered_paper_is_noted_once_per_version(conn):
    first = await run(conn)
    await paper(conn, "p", tier=1)
    await paper(conn, "quiet", tier=3)
    await conn.execute("UPDATE papers SET version = 1")
    await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
    await db.finish_run(conn, first, error=None)

    await conn.execute("UPDATE papers SET version = 2")
    second = await run(conn)
    digests = await write_run_digests(conn, Writer(), [FAITH_D], second, store=True)
    for d in digests:  # both streams, tier 3 silent, no new members
        assert "**Updated since we covered them**\n- [p](https://arxiv.org/abs/p) · v2" in d.body
        assert "quiet" not in d.body
    await db.finish_run(conn, second, error=None)

    third = await run(conn)
    digests = await write_run_digests(conn, Writer(), [FAITH_D], third, store=True)
    assert all("Updated since" not in d.body for d in digests)
    await db.finish_run(conn, third, error=None)
    await conn.execute("UPDATE papers SET version = 3")
    digests = await write_run_digests(conn, Writer(), [FAITH_D], await run(conn), store=False)
    assert all(" · v3, tier 1 in CoT faithfulness" in d.body for d in digests)


async def test_a_revision_awaiting_its_rescreen_is_not_noted_yet(conn):
    first = await run(conn)
    await paper(conn, "p", tier=1)
    await conn.execute("UPDATE papers SET version = 1")
    await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
    await db.finish_run(conn, first, error=None)
    # A rewritten revision cleared its screen; the old verdict must not announce it.
    await conn.execute("UPDATE papers SET version = 2, screened_at = NULL")
    second = await run(conn)
    digests = await write_run_digests(conn, Writer(), [FAITH_D], second, store=True)
    assert all("Updated since" not in d.body for d in digests)
    await db.finish_run(conn, second, error=None)
    await paper(conn, "p", tier=1)
    digests = await write_run_digests(conn, Writer(), [FAITH_D], await run(conn), store=True)
    assert all(" · v2, tier 1 in CoT faithfulness" in d.body for d in digests)


async def test_coverage_of_an_unknown_version_adopts_the_first_known_one(conn):
    first = await run(conn)
    await paper(conn, "p", tier=1)
    await write_run_digests(conn, Writer(), [FAITH_D], first, store=True)
    await db.finish_run(conn, first, error=None)
    await conn.execute("UPDATE papers SET version = 2")
    second = await run(conn)
    digests = await write_run_digests(conn, Writer(), [FAITH_D], second, store=True)
    assert all("Updated since" not in d.body for d in digests)  # unknown -> known: baseline
    await db.finish_run(conn, second, error=None)
    await conn.execute("UPDATE papers SET version = 3")
    digests = await write_run_digests(conn, Writer(), [FAITH_D], await run(conn), store=False)
    assert all(" · v3, tier 1 in CoT faithfulness" in d.body for d in digests)
