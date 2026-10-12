"""Papers triage keeps failing on: it stops after three nights, and the owner dismisses,
triages by hand, or fixes the text (spec decision 24)."""

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.store import papers as papers_store
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
