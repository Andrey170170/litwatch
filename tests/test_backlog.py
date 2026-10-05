"""The backlog drain against real PostgreSQL: what it takes, what it announces, how it yields."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from litwatch import db, ingest
from litwatch.config import Settings
from litwatch.screen import Cascade, ModelScreener
from tests.conftest import FAITH
from tests.test_calls import PAPER, Responses
from tests.test_digest_history import FAITH_D, Writer, write_run_digests

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="needs test PostgreSQL")
EDITED = FAITH.model_copy(update={"rubric": "A stricter rubric."})


@pytest.fixture
async def schema():
    async with await db.connect(os.environ["TEST_DATABASE_URL"]) as c:
        name = "test_backlog_" + uuid4().hex
        await c.execute(f'CREATE SCHEMA "{name}"')
        await c.execute(f'SET search_path TO "{name}"')
        await db.apply_schema(c)
        try:
            yield name
        finally:
            await c.execute(f'DROP SCHEMA "{name}" CASCADE')


@asynccontextmanager
async def connect(schema):
    async with await db.connect(os.environ["TEST_DATABASE_URL"]) as c:
        await c.execute(f'SET search_path TO "{schema}"')
        yield c


def papers(n):
    today = datetime.now(UTC).isoformat()
    return [PAPER.model_copy(update={"id": f"p{i}", "published": today}) for i in range(n)]


def cascade(selection, screener=None):
    return Cascade([selection], screener or ModelScreener(Responses().client(), "primary"))


async def test_an_edited_selection_is_backlog_not_the_runs_work_and_is_not_news(schema):
    settings = Settings("unused")
    async with connect(schema) as conn:
        await db.upsert_selection(conn, FAITH_D)
        await db.upsert_papers(conn, papers(1))
        first = await ingest.run(conn, settings, cascade(FAITH), "daily", 1, fetch=False)
        assert first.screened == 1
        await write_run_digests(conn, Writer(), [FAITH_D], first.run_id, store=True)
        # The paper became a member, but no issue has covered it for this stream.
        await conn.execute("DELETE FROM digest_coverage")

        edited = cascade(EDITED)
        again = await ingest.run(conn, settings, edited, "daily", 1, fetch=False)
        assert again.screened == 0
        assert [p.id for p in await db.backlog(conn, [EDITED])] == ["p0"]
        drained = await ingest.drain_backlog(conn, settings, edited, None, asyncio.Event())
        assert (drained.kind, drained.screened) == ("backlog", 1)
        assert await db.backlog(conn, [EDITED]) == []
        # Nothing left: no empty run row, and callers asking for their daily run never see one.
        assert await ingest.drain_backlog(conn, settings, edited, None, asyncio.Event()) is None
        assert [r["kind"] for r in await db.list_runs(conn)] == ["backlog", "daily", "daily"]
        assert [r["id"] for r in await db.list_runs(conn, 1, "daily")] == [again.run_id]

        # Judged again is not newly analyzed: the next issue does not announce the paper.
        later = await ingest.run(conn, settings, edited, "daily", 1, fetch=False)
        writer = Writer()
        await write_run_digests(conn, writer, [FAITH_D], later.run_id, store=True)
        assert writer.seen["general"] == []


async def test_a_run_waits_for_a_drain_which_gives_way_after_the_call_in_flight(schema):
    # One batch holds all three papers: the drain must let go inside it.
    settings = Settings("unused", backlog_batch=3, backlog_concurrency=1)
    gate, started = asyncio.Event(), asyncio.Event()

    class Slow(ModelScreener):
        async def screen(self, paper, selections):
            started.set()
            await gate.wait()
            return await super().screen(paper, selections)

    async with connect(schema) as a, connect(schema) as b, connect(schema) as watch:
        await db.upsert_papers(a, papers(3))
        assert (
            await ingest.run(a, settings, cascade(FAITH), "daily", 1, fetch=False)
        ).screened == 3
        slow = cascade(EDITED, Slow(Responses().client(), "primary"))
        drain = asyncio.create_task(ingest.drain_backlog(a, settings, slow, None, asyncio.Event()))
        await started.wait()
        nightly = asyncio.create_task(
            ingest.run(b, settings, cascade(EDITED), "daily", 1, fetch=False)
        )
        while not await ingest.run_waiting(watch):
            await asyncio.sleep(0.01)
        # The waiting run is on record before it has the lock.
        waiting = (await db.list_runs(watch, 1, "daily"))[0]
        assert waiting["finished_at"] is None
        gate.set()
        drained, ran = await drain, await nightly
        assert drained.screened == 1 and ran.run_id == waiting["id"] and ran.error is None
        assert len(await db.backlog(watch, [EDITED])) == 2


async def test_without_a_drain_a_busy_lock_still_fails_at_once(schema):
    async with connect(schema) as a, connect(schema) as b:
        await a.execute("SELECT pg_advisory_lock(%s)", (ingest.RUN_LOCK,))
        with pytest.raises(RuntimeError, match="already in progress"):
            await ingest.run(b, Settings("unused"), cascade(FAITH), "daily", 1, fetch=False)
        assert await db.list_runs(b) == []


async def test_a_new_selection_keeps_its_whole_window_across_an_interrupted_drain(
    schema, monkeypatch
):
    settings = Settings("unused", backlog_batch=1, backlog_concurrency=1)
    async with connect(schema) as conn:
        # No keyword of the new selection hits these papers: only its newness selects them.
        await db.upsert_papers(conn, papers(3))
        await conn.execute("UPDATE papers SET screened_at = now(), best_tier = 4")
        await db.upsert_selection(conn, FAITH)
        # A drain that finished an older version of the selection does not finish this one.
        await db.mark_window_screened(conn, {FAITH.slug: EDITED.version})
        # Asked before the batch, before its paper, and after it: a run arrives during it.
        waiting = iter([False, False, True])
        monkeypatch.setattr(ingest, "run_waiting", lambda conn: _answer(next(waiting, False)))
        first = await ingest.drain_backlog(conn, settings, cascade(FAITH), None, asyncio.Event())
        assert first.screened == 1
        # One verdict exists now; the rest of the window is still the selection's to judge.
        rest = await ingest.drain_backlog(conn, settings, cascade(FAITH), None, asyncio.Event())
        assert rest.screened == 2
        # Finished, the selection is an ordinary one: an edit takes members and keyword hits.
        await conn.execute("UPDATE verdicts SET tier = 4")
        assert await db.backlog(conn, [EDITED]) == []


async def _answer(value):
    return value
