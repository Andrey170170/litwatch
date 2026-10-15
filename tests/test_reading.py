"""The reading API: the status line, papers, the owner's word with its acknowledgements, and
misses."""

import re

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import Grade, Opinion, Verdict
from litwatch.feedback import word
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts
from tests.conftest import AGENTS, FAITH, TOPICS, paper

TOKEN = "s3cret"
AUTH = {"authorization": f"Bearer {TOKEN}"}
P = paper("2610.00001", title="Traces that hide steps", abstract="Hidden computation in CoT.")
Q = paper("2610.00002", title="A near neighbour", abstract="Sparse features.")


@pytest.fixture
async def client(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [P, Q])
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, Config())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=AUTH) as c:
        yield c


async def grade(conn, p, g: str) -> None:
    await verdicts.add(
        conn,
        Verdict(
            paper_id=p.id,
            topic=FAITH.slug,
            topic_revision=1,
            grade=Grade(g),
            judge=Opinion(grade=Grade(g), reason="judge's reason", model="anthropic:x"),
            check=Opinion(grade=Grade(g), reason="check's reason", model="openrouter:y"),
        ),
    )
    await word.settle(conn, p.id)


async def test_signing_in_trades_the_token_for_a_cookie(client):
    bare = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    assert (await bare.get("/api/status")).status_code == 401
    assert (await bare.post("/api/session", json={"token": "no"})).status_code == 401
    assert (await bare.post("/api/session", json={"token": TOKEN})).status_code == 204
    assert (await bare.get("/api/status")).json()["state"] == "none"


async def test_acknowledgements_say_what_happened(client, conn):
    await grade(conn, P, "relevant")
    said = await client.put(
        f"/api/reactions/{P.id}",
        json={"topic": FAITH.slug, "value": "down", "reason": "not_for_me"},
    )
    assert said.json() == {
        "message": "Remembered for CoT faithfulness",
        "saved": True,
        "grades": {},
    }
    said = await client.put(f"/api/reactions/{P.id}", json={"topic": AGENTS.slug, "value": "up"})
    assert said.json()["message"] == "Remembered for Agents"
    wrong = await client.put(f"/api/reactions/{P.id}", json={"value": "up", "reason": "overrated"})
    assert wrong.status_code == 422
    assert (await client.put(f"/api/library/{P.id}", json={"saved": False})).json()["message"] == (
        "Removed from the library"
    )
    assert (await client.put(f"/api/library/{Q.id}", json={"saved": True})).json()["saved"]


async def test_a_paper_page_carries_its_history_and_current_verdicts(client, conn):
    await grade(conn, P, "relevant")
    page = (await client.get(f"/api/papers/{P.id}")).json()
    (topic,) = page["topics"]
    assert (topic["slug"], topic["grade"]) == (FAITH.slug, "relevant")
    (chosen,) = page["chosen"]
    assert chosen["judge"] == {
        "grade": "relevant",
        "reason": "judge's reason",
        "model": "anthropic:x",
    }
    assert [h["what"] for h in page["history"]] == ["graded"]
    assert page["saved"] and page["pdf_url"] == f"https://arxiv.org/pdf/{P.id}"
    assert (await client.get("/api/papers/2610.99999")).status_code == 404


async def test_the_papers_table_filters_by_topic_and_searches(client, conn):
    await grade(conn, P, "relevant")
    await grade(conn, Q, "adjacent")
    rows = (await client.get("/api/papers", params={"topic": FAITH.slug})).json()
    assert [r["id"] for r in rows["rows"]] == [P.id] and rows["total"] == 1
    rows = (await client.get("/api/papers", params={"topic": FAITH.slug, "adjacent": True})).json()
    assert {r["id"] for r in rows["rows"]} == {P.id, Q.id}
    found = (await client.get("/api/papers", params={"q": "hidden computation"})).json()
    assert [r["id"] for r in found["rows"]] == [P.id]
    saved = (await client.get("/api/papers", params={"saved": True})).json()
    assert [r["id"] for r in saved["rows"]] == [P.id]
    bib = await client.post("/api/bibtex", json={"ids": [P.id]})
    assert "eprint = {2610.00001}" in bib.text


async def test_a_reported_miss_is_the_owners_word(client, conn):
    ack = await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    assert ack.json()["message"] == (
        "Reported as missed for CoT faithfulness; it joins the library"
    )
    (miss,) = await (await conn.execute("SELECT * FROM misses")).fetchall()
    assert (miss["paper_id"], miss["topic"]) == (Q.id, FAITH.slug)
    page = (await client.get(f"/api/papers/{Q.id}")).json()
    assert page["topics"][0]["label"]["origin"] == "miss"
    assert page["topics"][0]["missed"] and page["history"][0]["what"] == "missed"


async def test_the_status_line_follows_the_latest_nightly_run(client, conn):
    assert (await client.get("/api/status")).json() == {
        "state": "none",
        "run_id": None,
        "started_at": None,
        "finished_at": None,
        "error": None,
    }
    await conn.execute("INSERT INTO runs (kind) VALUES ('nightly')")
    assert (await client.get("/api/status")).json()["state"] == "running"
    await conn.execute("UPDATE runs SET finished_at = now()")
    assert (await client.get("/api/status")).json()["state"] == "quiet"
    await conn.execute("UPDATE runs SET error = 'boom'")
    status = (await client.get("/api/status")).json()
    assert (status["state"], status["error"]) == ("failed", "boom")
    # A run that never finished and was started long ago failed too.
    await conn.execute(
        "UPDATE runs SET error = NULL, finished_at = NULL, started_at = '2020-01-01'"
    )
    assert (await client.get("/api/status")).json()["state"] == "failed"


async def test_a_paper_without_authors_still_reads(client, conn):
    await conn.execute("UPDATE papers SET authors = 'null'::jsonb WHERE id = %s", (P.id,))
    rows = (await client.get("/api/papers", params={"q": "hidden computation"})).json()["rows"]
    assert rows[0]["authors"] == []
    assert (await client.get(f"/api/papers/{P.id}")).json()["authors"] == []
    assert "@misc" in (await client.post("/api/bibtex", json={"ids": [P.id]})).text


async def test_reporting_a_miss_again_keeps_a_later_correction(client, conn):
    await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    await client.put(f"/api/word/{FAITH.slug}/{Q.id}", json={"value": "not_relevant"})
    again = await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    assert again.json()["message"] == "Already reported as missed for CoT faithfulness"
    (label,) = await (await conn.execute("SELECT value FROM labels")).fetchall()
    assert label["value"] == "not_relevant"
    assert len(await (await conn.execute("SELECT id FROM misses")).fetchall()) == 1


async def test_a_page_past_the_end_still_counts_the_filter(client, conn):
    await grade(conn, P, "relevant")
    page = (await client.get("/api/papers", params={"topic": FAITH.slug, "offset": 50})).json()
    assert (page["rows"], page["total"]) == ([], 1)


async def test_the_cookie_is_secure_behind_https(client):
    signed = await client.post(
        "/api/session", json={"token": TOKEN}, headers={"x-forwarded-proto": "https"}
    )
    assert "secure" in signed.headers["set-cookie"].lower()
    plain = await client.post("/api/session", json={"token": TOKEN})
    assert "secure" not in plain.headers["set-cookie"].lower()


def test_lookup_reads_old_style_ids():
    from litwatch.intake.search import id_from_url

    assert id_from_url("https://arxiv.org/abs/cs/9901001v2") == "cs/9901001"
    assert id_from_url("math.AG/0601001") == "math.AG/0601001"
    assert id_from_url("arXiv:2610.00001v3") == "2610.00001"


async def test_the_paper_page_lists_versions_seen(client, conn):
    await conn.execute(
        'UPDATE papers SET version_history = \'[{"version": 1}, {"version": 2}]\' WHERE id = %s',
        (P.id,),
    )
    assert [v["version"] for v in (await client.get(f"/api/papers/{P.id}")).json()["versions"]] == [
        1,
        2,
    ]


async def test_also_in_leaves_out_the_topic_whose_table_it_is(client, conn):
    await grade(conn, P, "relevant")
    (row,) = (await client.get("/api/papers", params={"q": "hidden computation"})).json()["rows"]
    assert (row["grade"], row["also_in"]) == ("relevant", ["CoT faithfulness"])
    (row,) = (await client.get("/api/papers", params={"topic": FAITH.slug})).json()["rows"]
    assert row["also_in"] == []


async def test_bibtex_covers_papers_a_provider_found_outside_the_collection(client, conn):
    """A search hit the collection lacks gets an arXiv entry from what the hit carries,
    beside the collection's papers, without being fetched (decision 6)."""
    hit = {
        "id": "2610.99901",
        "title": "Found by the archive",
        "authors": ["Ada Lovelace"],
        "year": 2026,
    }
    text = (await client.post("/api/bibtex", json={"ids": [P.id], "cited": [hit]})).text
    assert text.count("@misc") == 2
    assert "eprint = {2610.99901}" in text and "year = {2026}" in text
    assert "author = {Ada Lovelace}" in text
    assert await papers_store.get(conn, "2610.99901") is None
    # A hit that is also chosen from the collection is not cited twice.
    hit["id"] = P.id
    text = (await client.post("/api/bibtex", json={"ids": [P.id], "cited": [hit]})).text
    assert text.count("@misc") == 1
    # The collection's entries and the hits share one key namespace: no duplicate keys.
    twin = {"id": "2610.99902", "title": "Traces elsewhere", "authors": P.authors, "year": None}
    text = (await client.post("/api/bibtex", json={"ids": [P.id], "cited": [twin]})).text
    keys = re.findall(r"^@\w+\{([^,]+),", text, re.MULTILINE)
    assert len(keys) == 2 and len(set(keys)) == 2
    # A table filter is resolved on the server, in the same request as the band's hits.
    body = {"filter": {"q": "hidden computation"}, "cited": [twin]}
    text = (await client.post("/api/bibtex", json=body)).text
    assert "eprint = {2610.00001}" in text and "eprint = {2610.99902}" in text
    # A filter kept to one band exports that band only, as the table shows it.
    body["filter"]["source"] = "outside"
    text = (await client.post("/api/bibtex", json=body)).text
    assert "eprint = {2610.00001}" not in text and "eprint = {2610.99902}" in text
    body["filter"]["source"] = "collection"
    text = (await client.post("/api/bibtex", json=body)).text
    assert "eprint = {2610.00001}" in text and "eprint = {2610.99902}" not in text


async def test_a_miss_taken_back_can_be_reported_again(client, conn):
    """Taking a miss back withdraws the owner's word; reporting again gives it back, keeping
    the miss, and never over a correction the owner made since."""

    async def faith():
        # A topic with nothing to say about the paper is left off the page.
        page = (await client.get(f"/api/papers/{Q.id}")).json()
        return next((t for t in page["topics"] if t["slug"] == FAITH.slug), {"missed": False})

    await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    (miss,) = await (await conn.execute("SELECT id FROM misses")).fetchall()
    assert (await faith())["missed"]
    await client.delete(f"/api/word/{FAITH.slug}/{Q.id}")
    assert not (await faith())["missed"]
    ack = await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    assert ack.json()["message"].startswith("Already reported as missed")
    assert (await faith())["missed"] and (await faith())["label"]["origin"] == "miss"
    assert [m["id"] for m in await (await conn.execute("SELECT id FROM misses")).fetchall()] == [
        miss["id"]
    ]
    # A newer thumb withdraws it as the owner's word, though the relevant label is still stored.
    body = {"topic": FAITH.slug, "value": "down", "reason": "unrelated"}
    await client.put(f"/api/reactions/{Q.id}", json=body)
    assert not (await faith())["missed"]
    await client.delete(f"/api/reactions/{Q.id}", params={"topic": FAITH.slug})
    assert (await faith())["missed"]
    # A correction since stands: the owner said it is not relevant after all.
    await client.put(f"/api/word/{FAITH.slug}/{Q.id}", json={"value": "not_relevant"})
    await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    assert (await faith())["label"]["value"] == "not_relevant" and not (await faith())["missed"]


async def test_reporting_a_miss_again_waits_for_a_correction_in_flight(client, conn):
    """Looking for the owner's standing word and giving it again happen under the paper's
    lock, so a correction being written meanwhile is seen, and stands."""
    import asyncio

    from litwatch import store
    from litwatch.core import Label, LabelOrigin, LabelValue
    from litwatch.feedback import word as word_feedback
    from litwatch.store import feedback as feedback_store

    await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    await client.delete(f"/api/word/{FAITH.slug}/{Q.id}")
    async with await store.sibling(conn) as other:
        async with other.transaction():
            await word_feedback.lock(other, Q.id)  # a correction, part way through
            again = asyncio.create_task(
                client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
            )
            await asyncio.sleep(0.5)
            assert not again.done()
            await feedback_store.set_label(
                other,
                Label(
                    paper_id=Q.id,
                    topic=FAITH.slug,
                    value=LabelValue.NOT_RELEVANT,
                    origin=LabelOrigin.READER,
                ),
            )
        assert (await again).status_code == 200
    (label,) = await (await conn.execute("SELECT value, origin FROM labels")).fetchall()
    assert (label["value"], label["origin"]) == ("not_relevant", "reader")


async def test_a_topic_card_counts_its_papers_and_how_many_are_saved(client, conn):
    await grade(conn, P, "relevant")
    await grade(conn, Q, "adjacent")
    cards = {c["slug"]: c for c in (await client.get("/api/topics")).json()}
    assert (cards[FAITH.slug]["papers"], cards[FAITH.slug]["saved"]) == (1, 1)
