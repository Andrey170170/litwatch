"""The reading API: issues, papers, the owner's word with its acknowledgements, and misses."""

import json

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import Grade, Opinion, Verdict
from litwatch.feedback import word
from litwatch.jobs import digest
from litwatch.jobs.context import model_run
from litwatch.llm import Gateway
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts
from tests.conftest import AGENTS, FAITH, TOPICS, paper
from tests.fakes import ScriptedProvider

TOKEN = "s3cret"
AUTH = {"authorization": f"Bearer {TOKEN}"}
P = paper("2610.00001", title="Traces that hide steps", abstract="Hidden computation in CoT.")
Q = paper("2610.00002", title="A near neighbour", abstract="Sparse features.")


def respond(role, request):
    if "You write for litwatch" in request.system:
        key = "lede" if "lede" in request.system else "take"
        return json.dumps({key: f"A {key}."})
    if "sort arXiv papers" in request.system:
        return json.dumps(
            {
                "cot_faithfulness": {"why": "w", "relation": "unrelated"},
                "interpretability": {"yes": False},
                "agents": {"yes": False},
            }
        )
    return json.dumps({"reason": "hides steps", "grade": "relevant"})


def gateway(config, journal):
    providers = {n: ScriptedProvider(n, respond) for n in ("anthropic", "openrouter")}
    return Gateway(config.models, providers, journal, retry_delays=())


@pytest.fixture
async def client(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [P, Q])
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, Config(), gateway=gateway)
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


async def night(conn) -> None:
    async with model_run(conn, Config(), "nightly", gateway=gateway) as run:
        await digest.write_takes(conn, Config(), run)
        await digest.build_daily(conn, Config(), run)


async def test_signing_in_trades_the_token_for_a_cookie(client):
    bare = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    assert (await bare.get("/api/status")).status_code == 401
    assert (await bare.post("/api/session", json={"token": "no"})).status_code == 401
    assert (await bare.post("/api/session", json={"token": TOKEN})).status_code == 204
    assert (await bare.get("/api/status")).json()["state"] == "none"


async def test_an_issue_reads_as_sections_and_strikes_through_what_was_set_aside(client, conn):
    await grade(conn, P, "must_read")
    await grade(conn, Q, "adjacent")
    await night(conn)
    feed = (await client.get("/api/digest")).json()
    (summary,) = feed["issues"]
    assert summary["lede"] == "A lede." and feed["status"]["state"] == "quiet"
    issue = (await client.get(f"/api/issues/{summary['id']}")).json()
    (section,) = issue["sections"]
    assert section["topic"]["slug"] == FAITH.slug and section["near"] == 1
    (entry,) = section["entries"]
    assert (entry["lead"], entry["take"], entry["set_aside"]) == (True, "A take.", False)

    ack = await client.put(
        f"/api/reactions/{P.id}",
        json={"topic": FAITH.slug, "value": "down", "reason": "unrelated"},
    )
    assert (
        ack.json()["message"] == "Marked not relevant for CoT faithfulness; it leaves the library"
    )
    issue = (await client.get(f"/api/issues/{summary['id']}")).json()
    assert issue["sections"][0]["entries"][0]["set_aside"] is True


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


async def test_a_paper_page_carries_takes_history_and_how_it_was_chosen(client, conn):
    await grade(conn, P, "relevant")
    await night(conn)
    page = (await client.get(f"/api/papers/{P.id}")).json()
    (topic,) = page["topics"]
    assert (topic["slug"], topic["grade"], topic["take"]) == (FAITH.slug, "relevant", "A take.")
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


async def test_a_reported_miss_is_the_owners_word_and_is_traced(client, conn):
    ack = await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    assert ack.json()["message"] == (
        "Reported as missed for CoT faithfulness; it joins the library"
    )
    (miss,) = await (await conn.execute("SELECT * FROM misses")).fetchall()
    assert miss["eval_run_id"] is not None
    (result,) = await (
        await conn.execute(
            "SELECT * FROM eval_results WHERE eval_run_id = %s AND topic = %s",
            (miss["eval_run_id"], FAITH.slug),
        )
    ).fetchall()
    assert (result["triage_yes"], result["grade"]) == (False, "relevant")
    # The writer takes its notes from the trace, since there is no verdict.
    await night(conn)
    (take,) = await (await conn.execute("SELECT source FROM takes")).fetchall()
    assert take["source"] == "evaluation"
    page = (await client.get(f"/api/papers/{Q.id}")).json()
    assert page["topics"][0]["label"]["origin"] == "miss"


async def test_the_status_line_counts_what_waits_for_the_owner(client, conn):
    # A split that straddles the relevant line, in the latest issue.
    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id,
            topic=FAITH.slug,
            topic_revision=1,
            grade=Grade.RELEVANT,
            split=True,
            judge=Opinion(grade=Grade.RELEVANT, reason="r"),
            check=Opinion(grade=Grade.ADJACENT, reason="r"),
        ),
    )
    await word.settle(conn, P.id)
    await night(conn)
    status = (await client.get("/api/status")).json()
    assert (status["state"], status["splits"], status["read"]) == ("attention", 1, 0)
    # The owner's word settles it.
    await client.put(f"/api/word/{FAITH.slug}/{P.id}", json={"value": "relevant"})
    assert (await client.get("/api/status")).json()["state"] == "quiet"
    # A failed run turns the line red and says which issue is shown.
    await conn.execute("UPDATE runs SET error = 'boom'")
    status = (await client.get("/api/status")).json()
    assert status["state"] == "failed" and status["showing"] is not None


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
    runs = await (await conn.execute("SELECT id FROM eval_runs WHERE kind = 'miss'")).fetchall()
    assert len(runs) == 1


async def test_a_miss_over_a_rejecting_verdict_takes_its_notes_from_the_trace(client, conn):
    await grade(conn, Q, "none")
    await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    await night(conn)
    (take,) = await (await conn.execute("SELECT source FROM takes")).fetchall()
    assert take["source"] == "evaluation"


async def test_a_trace_that_fails_is_recorded_on_the_miss(conn):
    from litwatch.feedback import misses

    def broken(config, journal):
        raise RuntimeError("no provider")

    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [Q])
    miss_id, *_ = await misses.report(conn, Q.id, FAITH.slug)
    assert await misses.trace(conn, Config(), miss_id, gateway=broken) is None
    row = await (await conn.execute("SELECT trace, trace_error FROM misses")).fetchone()
    assert row["trace"] == "failed" and "no provider" in row["trace_error"]


async def test_amendments_count_as_new_on_a_topic_opened_in_between(client, conn):
    await grade(conn, P, "relevant")
    await night(conn)
    await client.post(f"/api/topics/{FAITH.slug}/visit")
    await grade(conn, Q, "relevant")
    await night(conn)  # the same day: the issue is amended
    card = next(t for t in (await client.get("/api/topics")).json() if t["slug"] == FAITH.slug)
    assert card["new"] == 1


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


async def test_a_misss_take_waits_for_its_trace_and_is_rewritten_with_it(conn):
    from litwatch.feedback import misses

    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [Q])
    miss_id, *_ = await misses.report(conn, Q.id, FAITH.slug)
    assert await digest._owed(conn) == []  # waiting, not failing
    (due,) = await (await conn.execute("SELECT failed_runs FROM due")).fetchall()
    assert due["failed_runs"] == 0
    # A take written in the meantime from the owner's word alone is owed again with the notes.
    await conn.execute("UPDATE misses SET trace = 'failed'")
    await night(conn)
    await misses.trace(conn, Config(), miss_id, gateway=gateway)
    (due,) = await (await conn.execute("SELECT for_take FROM due")).fetchall()
    assert due["for_take"] is True


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


async def test_a_take_owed_a_rewrite_is_not_shown_until_it_is_written(client, conn):
    await grade(conn, P, "relevant")
    await night(conn)
    await grade(conn, P, "must_read")  # the short take no longer fits
    page = (await client.get(f"/api/papers/{P.id}")).json()
    assert page["topics"][0]["take"] is None
    await night(conn)
    assert (await client.get(f"/api/papers/{P.id}")).json()["topics"][0]["take"] == "A take."


async def test_reporting_again_traces_a_miss_whose_trace_never_finished(client, conn):
    from litwatch.feedback import misses

    await misses.report(conn, Q.id, FAITH.slug)  # its trace was cut off
    await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    row = await (await conn.execute("SELECT trace FROM misses")).fetchone()
    assert row["trace"] == "done"


async def test_a_trace_of_rewritten_text_gives_no_notes(client, conn):
    from litwatch.feedback import misses

    await client.post("/api/misses", json={"paper_id": Q.id, "topic": FAITH.slug})
    await conn.execute("UPDATE papers SET revision = 2 WHERE id = %s", (Q.id,))
    rewritten = await papers_store.get(conn, Q.id)
    assert await misses.opinions(conn, rewritten, FAITH.model_copy(update={"revision": 1})) == []


async def test_a_trace_cut_off_long_ago_stops_holding_the_take_back(conn):
    from litwatch.feedback import misses

    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [Q])
    await misses.report(conn, Q.id, FAITH.slug)
    assert await digest._owed(conn) == []
    await conn.execute("UPDATE misses SET reported_at = now() - interval '2 hours'")
    (item,) = await digest._owed(conn)
    assert item.source == "owner"


async def test_a_trace_already_running_is_not_started_twice(conn):
    from litwatch.feedback import misses

    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [Q])
    miss_id, *_ = await misses.report(conn, Q.id, FAITH.slug)
    await conn.execute("UPDATE misses SET trace_started = now()")  # one is running
    assert await misses.trace(conn, Config(), miss_id, gateway=gateway) is None
    *_, needs_trace = await misses.report(conn, Q.id, FAITH.slug)
    assert needs_trace is False


async def test_a_word_given_before_a_new_split_does_not_silence_it(client, conn):
    await client.put(f"/api/word/{FAITH.slug}/{P.id}", json={"value": "relevant"})
    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id,
            topic=FAITH.slug,
            topic_revision=1,
            grade=Grade.RELEVANT,
            split=True,
            judge=Opinion(grade=Grade.RELEVANT, reason="r"),
            check=Opinion(grade=Grade.NONE, reason="r"),
        ),
    )
    await word.settle(conn, P.id)
    await conn.execute("INSERT INTO issues (kind, day) VALUES ('daily', current_date)")
    await conn.execute(
        "INSERT INTO issue_entries (issue_id, paper_id, topic, grade, position) "
        "SELECT id, %s, %s, 'relevant', 1 FROM issues",
        (P.id, FAITH.slug),
    )
    await conn.execute("INSERT INTO runs (kind, finished_at) VALUES ('nightly', now())")
    assert (await client.get("/api/status")).json()["splits"] == 1


async def test_a_row_says_whose_take_it_shows_and_leaves_that_topic_out_of_also_in(client, conn):
    await grade(conn, P, "relevant")
    await night(conn)
    (row,) = (await client.get("/api/papers", params={"q": "hidden computation"})).json()["rows"]
    assert (row["take"], row["take_topic"], row["also_in"]) == ("A take.", FAITH.slug, [])


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
