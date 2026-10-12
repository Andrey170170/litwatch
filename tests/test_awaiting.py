"""Papers triage keeps failing on: it stops after three nights, and the owner dismisses,
triages by hand, or fixes the text (spec decision 24)."""

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import Candidate, Grade, Take, TakeSource, Verdict
from litwatch.feedback import word
from litwatch.store import digest as digest_store
from litwatch.store import papers as papers_store
from litwatch.store import quality, verdicts
from litwatch.store import topics as topics_store
from litwatch.store import triage as triage_store
from tests.conftest import FAITH, INTERP, TOPICS, paper

TOKEN = "s3cret"
STUCK = paper("2610.00009", title="Broken title", abstract="Garbled abstract.", version=1)
FINE = paper("2610.00010", title="Fine", abstract="Fine.")


@pytest.fixture
async def client(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [STUCK, FINE])
    await conn.execute(
        "INSERT INTO triage (paper_id, state, failed_runs, last_error) "
        "VALUES (%s, 'failed', 3, 'invalid: not JSON')",
        (STUCK.id,),
    )
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    transport = httpx.ASGITransport(app=create_app(dsn, TOKEN, Config()))
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def untriaged(conn):
    return {p.id for p in await triage_store.untriaged(conn, None, attempts=3)}


async def test_triage_stops_after_three_failed_nights(client, conn):
    assert await untriaged(conn) == {FINE.id}
    quality = (await client.get("/api/settings/quality")).json()
    assert [p["paper_id"] for p in quality["night"]["awaiting_triage"]] == [STUCK.id]


async def test_a_dismissed_paper_is_never_triaged_and_stops_waiting(client, conn):
    assert (await client.post(f"/api/settings/triage/{STUCK.id}/dismiss")).status_code == 204
    assert await untriaged(conn) == {FINE.id}
    quality = (await client.get("/api/settings/quality")).json()
    assert quality["night"]["awaiting_triage"] == []
    assert (await client.post(f"/api/settings/triage/{FINE.id}/dismiss")).status_code == 404


async def test_a_dismissed_paper_is_not_graded_or_triaged_after_a_rewrite(client, conn):
    # The keyword audit had made it a candidate before triage gave up.
    await triage_store.put_candidate(
        conn,
        Candidate(
            paper_id=STUCK.id, topic=FAITH.slug, topic_revision=1, paper_revision=1, by_keyword=True
        ),
    )
    assert [p.id for _, p in await triage_store.pending_candidates(conn, 3)] == [STUCK.id]
    await client.post(f"/api/settings/triage/{STUCK.id}/dismiss")
    assert await triage_store.pending_candidates(conn, 3) == []
    rewritten = STUCK.model_copy(update={"abstract": "An entirely new abstract.", "version": 2})
    assert (await papers_store.upsert(conn, [rewritten])).rewritten == [STUCK.id]
    assert await untriaged(conn) == {FINE.id}
    # Nor does a re-run or grade-now reach it.
    assert await quality.candidate_papers(conn, FAITH.slug) == []
    assert await quality.newest_candidates(conn, FAITH.slug) == []
    assert STUCK.id not in await quality.keyword_papers(conn, ["abstract"])


async def test_triage_by_hand_puts_the_paper_forward_as_the_owners(client, conn):
    answers = {FAITH.slug: True, INTERP.slug: True}
    made = await client.put(f"/api/settings/triage/{STUCK.id}", json={"answers": answers})
    assert made.json() == {"candidates": [FAITH.slug]}
    candidate = await (await conn.execute("SELECT * FROM candidates")).fetchone()
    assert (candidate["by_owner"], candidate["by_triage"], candidate["state"]) == (
        True,
        False,
        "pending",
    )
    member = await (await conn.execute("SELECT topic FROM memberships")).fetchone()
    assert member["topic"] == INTERP.slug
    assert await untriaged(conn) == {FINE.id}  # triaged now, by the owner
    again = await client.put(f"/api/settings/triage/{STUCK.id}", json={"answers": {}})
    assert again.status_code == 404  # no longer awaiting


async def test_a_fixed_text_is_triaged_again_and_kept_until_a_newer_version(client, conn):
    fixed = {"title": "Fixed title"}
    sent = await client.post(f"/api/settings/triage/{STUCK.id}/correction", json=fixed)
    assert sent.status_code == 204
    stored = await papers_store.get(conn, STUCK.id)
    assert (stored.title, stored.revision) == ("Fixed title", 2)
    assert STUCK.id in await untriaged(conn)  # its failures are forgotten
    # Refetching the same version keeps the correction, and is no rewrite.
    result = await papers_store.upsert(conn, [STUCK])
    assert result.rewritten == []
    assert (await papers_store.get(conn, STUCK.id)).title == "Fixed title"
    # A newer version replaces it.
    await papers_store.upsert(conn, [STUCK.model_copy(update={"title": "New title", "version": 2})])
    assert (await papers_store.get(conn, STUCK.id)).title == "New title"


async def test_a_correction_is_keyed_on_the_text_not_the_version(client, conn):
    unknown = STUCK.model_copy(update={"version": None})
    await conn.execute("UPDATE papers SET version = NULL WHERE id = %s", (STUCK.id,))
    await client.post(f"/api/settings/triage/{STUCK.id}/correction", json={"title": "Fixed"})
    # The same text again, still with no version known: the correction stands.
    await papers_store.upsert(
        conn, [unknown.model_copy(update={"abstract": " Garbled  abstract."})]
    )
    assert (await papers_store.get(conn, STUCK.id)).title == "Fixed"
    # New text from arXiv, version or not, replaces it.
    await papers_store.upsert(conn, [unknown.model_copy(update={"title": "Retitled"})])
    assert (await papers_store.get(conn, STUCK.id)).title == "Retitled"


async def test_a_correction_makes_the_papers_takes_stale(client, conn):
    verdict = Verdict(paper_id=STUCK.id, topic=FAITH.slug, topic_revision=1, grade=Grade.RELEVANT)
    await verdicts.add(conn, verdict)
    await digest_store.add_take(
        conn,
        Take(
            paper_id=STUCK.id,
            topic=FAITH.slug,
            paper_revision=1,
            topic_revision=1,
            grade=Grade.RELEVANT,
            source=TakeSource.VERDICT,
            text="On the broken text.",
        ),
    )
    await word.settle(conn, STUCK.id)

    async def owed():
        row = await (
            await conn.execute("SELECT for_take FROM due WHERE paper_id = %s", (STUCK.id,))
        ).fetchone()
        return row["for_take"]

    assert not await owed()
    await client.post(f"/api/settings/triage/{STUCK.id}/correction", json={"title": "Fixed"})
    assert await owed()  # written from the old text


async def test_a_correction_of_only_whitespace_is_refused(client, conn):
    blank = await client.post(f"/api/settings/triage/{STUCK.id}/correction", json={"title": "  "})
    assert blank.status_code == 422
    assert (await papers_store.get(conn, STUCK.id)).title == "Broken title"
