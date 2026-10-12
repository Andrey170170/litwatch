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
