"""Acknowledge on Needs you (DESIGN.md, Needs you; decision 72): the owner marks stopped steps,
full-text passes and failed digests seen, each as the page showed it, and they stop counting in
the run status. One that recovers and fails again comes back; a failed nightly run is never
acknowledged."""

from datetime import date, datetime, timedelta

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import FAILED_RUNS, Fit, Verdict
from litwatch.core.digests import DigestPeriod, DigestState
from litwatch.feedback import word
from litwatch.store import digests as digests_store
from litwatch.store import failures, verdicts
from litwatch.store import fulltext as fulltext_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import FAITH, TREE, paper

TOKEN = "s3cret"
P = paper("2610.00001", title="Traces that hide steps", abstract="Hidden computation in CoT.")
Q = paper("2610.00002", title="A near neighbour", abstract="Sparse features.")
R = paper("2610.00003", title="Probing a trace", abstract="Linear probes on CoT.")
DAY = date(2026, 10, 18)
STOPPED = ("awaiting_classification", "analysis_failed", "ungraded", "fulltext_failed")


@pytest.fixture
async def client(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P, Q, R])
    await conn.execute(
        "INSERT INTO runs (kind, started_at, finished_at) VALUES ('nightly', now(), now())"
    )
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, Config())
    transport = httpx.ASGITransport(app=app)
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def stop(conn, p, step: str, topic: str = "") -> None:
    """Three unusable answers in a row: the step waits for the owner."""
    for _ in range(FAILED_RUNS):
        await failures.failed(conn, p, step, "unusable", counts=True, topic=topic)


async def fail_pass(conn, pass_id: int) -> None:
    """The pass fails three runs, as the worker records them."""
    for _ in range(FAILED_RUNS):
        await conn.execute("UPDATE fulltext_passes SET state = 'ready' WHERE id = %s", (pass_id,))
        (claimed,) = await fulltext_store.claim(conn, timedelta(minutes=5))
        await fulltext_store.failed(conn, claimed, "no text to be had", counts=True)


async def stop_pass(conn) -> int:
    """R central on the Focus topic gets a Brief pass, which fails three runs."""
    await conn.execute(
        "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
        "primary_class, model) VALUES (%s, 1, 'x', 'interpretability', 'm')",
        (R.id,),
    )
    central = Verdict(
        paper_id=R.id, topic=FAITH.slug, topic_version="v", fit=Fit.CENTRAL, reason="r",
        opinions={},
    )  # fmt: skip
    await verdicts.add(conn, central)
    await word.settle(conn, R.id)
    (row,) = await (
        await conn.execute("SELECT id FROM fulltext_passes WHERE kind = 'brief'")
    ).fetchall()
    await fail_pass(conn, row["id"])
    return row["id"]


async def fail_digest(conn, day: date = DAY) -> int:
    o = digests_store.Outcome(FAITH.slug, DigestPeriod.DAY, day, DigestState.FAILED, error="x")
    return await digests_store.record(conn, o, None)


async def everything(conn) -> tuple[int, int]:
    """One item in each red group of Needs you: the pass's and the digest's ids."""
    await stop(conn, P, "classification")
    await stop(conn, Q, "analysis")
    await stop(conn, Q, "grading", FAITH.slug)
    return await stop_pass(conn), await fail_digest(conn)


LISTS = {"awaiting_classification": "classification", "analysis_failed": "analysis"}
LISTS |= {"ungraded": "grading"}
ALL = (*LISTS, "fulltext", "digests")


async def shown(client, *lists: str) -> dict:
    """What Needs you names on Acknowledge for `lists`: their rows not yet acknowledged, each
    as the page shows it."""
    night = (await client.get("/api/settings/quality")).json()["night"]
    rows = {k: [x for x in night[k] if not x["acknowledged_at"]] for k in lists}
    steps = [
        {k: x[k] for k in ("paper_id", "revision", "topic", "failed_at")} | {"step": LISTS[k]}
        for k in LISTS
        for x in rows.get(k, [])
    ]
    passes = [{"id": x["id"], "since": x["since"]} for x in rows.get("fulltext", [])]
    digests = [x["id"] for x in rows.get("digests", [])]
    return {"steps": steps, "passes": passes, "digests": digests}


async def ack(client, body: dict) -> dict:
    """Acknowledge `body`; the API's answer, what it marked."""
    answer = await client.post("/api/settings/acknowledge", json=body)
    assert answer.status_code == 200
    return answer.json()


NOTHING = {"steps": [], "passes": [], "digests": []}


async def status(client) -> dict:
    return (await client.get("/api/status")).json()


async def test_acknowledge_per_group_then_all_clears_the_run_status(client, conn):
    await everything(conn)
    before = await status(client)
    assert before["state"] == "attention"
    assert [before[k] for k in (*STOPPED, "digests_failed")] == [1, 1, 1, 1, 1]
    assert (await client.get("/api/summary")).json()["settings"]["waiting"] == 5

    # One group: only its count drops; the item stays listed, marked. The API answers with
    # what it marked, as named.
    group = await shown(client, "ungraded")
    assert len(group["steps"]) == 1
    marked = await ack(client, group)
    assert [{**x, "failed_at": None} for x in marked["steps"]] == [
        {**x, "failed_at": None} for x in group["steps"]
    ]
    assert datetime.fromisoformat(marked["steps"][0]["failed_at"]) == datetime.fromisoformat(
        group["steps"][0]["failed_at"]
    )
    after = await status(client)
    assert (after["state"], after["ungraded"], after["analysis_failed"]) == ("attention", 0, 1)
    night = (await client.get("/api/settings/quality")).json()["night"]
    (ungraded,) = night["ungraded"]
    assert ungraded["acknowledged_at"] is not None
    assert night["analysis_failed"][0]["acknowledged_at"] is None

    # Acknowledge all: the status line shows the night's papers again.
    rest = await shown(client, *ALL)
    marked = await ack(client, rest)
    assert [len(marked[k]) for k in ("steps", "passes", "digests")] == [2, 1, 1]
    # The same again (its answer lost, or from another tab) answers the same, keeping the mark.
    first = await (await conn.execute("SELECT acknowledged_at FROM digests")).fetchone()
    assert await ack(client, rest) == marked
    again = await (await conn.execute("SELECT acknowledged_at FROM digests")).fetchone()
    assert again == first
    quiet = await status(client)
    assert quiet["state"] == "quiet"
    assert [quiet[k] for k in (*STOPPED, "digests_failed")] == [0, 0, 0, 0, 0]
    assert (await client.get("/api/summary")).json()["settings"]["waiting"] == 0
    night = (await client.get("/api/settings/quality")).json()["night"]
    assert all(len(night[k]) == 1 and night[k][0]["acknowledged_at"] for k in ALL)


async def test_only_what_still_waits_as_shown_is_acknowledged(client, conn):
    await stop(conn, P, "analysis")
    (seen,) = (await shown(client, "analysis_failed"))["steps"]
    # Named for another revision (the text changed under the page), or a step never stopped.
    other = {**seen, "revision": 2}
    never = {**seen, "step": "classification", "paper_id": Q.id}
    assert await ack(client, {**NOTHING, "steps": [other, never]}) == NOTHING
    assert (await status(client))["analysis_failed"] == 1
    (row,) = await (await conn.execute("SELECT acknowledged_at FROM step_failures")).fetchall()
    assert row["acknowledged_at"] is None


async def test_a_page_older_than_the_failure_acknowledges_nothing(client, conn):
    """The page showed a failure; meanwhile it was tried again and failed again. Acknowledge
    from that page names the earlier failure, and marks nothing: the owner has not seen this
    one. Nor is a failed digest of a topic no longer active marked."""
    pass_id, _ = await everything(conn)
    stale = await shown(client, *ALL)
    retried = await client.post(f"/api/settings/analysis/{Q.id}/retry", json={"revision": 1})
    assert retried.status_code == 204
    await stop(conn, Q, "analysis")
    assert (await client.post(f"/api/settings/fulltext/{pass_id}/now")).status_code == 204
    await fail_pass(conn, pass_id)
    await conn.execute("UPDATE topics SET status = 'paused' WHERE class = %s", (FAITH.slug,))

    marked = await ack(client, stale)
    # The classification still waits as shown; the analysis and the pass failed again since,
    # and the paused topic's digest and grading no longer wait.
    assert [x["step"] for x in marked["steps"]] == ["classification"]
    assert (marked["passes"], marked["digests"]) == ([], [])
    back = await status(client)
    assert (back["analysis_failed"], back["fulltext_failed"]) == (1, 1)


async def test_an_item_that_recovers_and_fails_again_comes_back(client, conn):
    pass_id, digest_id = await everything(conn)
    await ack(client, await shown(client, *ALL))
    assert (await status(client))["state"] == "quiet"

    # Try again on the analysis: it recovers, then fails three more runs.
    retried = await client.post(f"/api/settings/analysis/{Q.id}/retry", json={"revision": 1})
    assert retried.status_code == 204
    await stop(conn, Q, "analysis")
    # Grade it now forgets the count (`failures.reset`); the grading fails three more runs.
    await failures.reset(conn, Q.id, "grading", FAITH.slug)
    await stop(conn, Q, "grading", FAITH.slug)
    # Run it now on the pass, which fails three more runs.
    assert (await client.post(f"/api/settings/fulltext/{pass_id}/now")).status_code == 204
    await fail_pass(conn, pass_id)
    back = await status(client)
    assert back["state"] == "attention"
    assert (back["analysis_failed"], back["ungraded"], back["fulltext_failed"]) == (1, 1, 1)
    # The classification was never retried: it stays acknowledged.
    assert back["awaiting_classification"] == 0

    # A digest that fails again without being written stays acknowledged; once written, a
    # later day's failure is a new item.
    assert await fail_digest(conn) == digest_id
    assert (await status(client))["digests_failed"] == 0
    written = digests_store.Outcome(FAITH.slug, DigestPeriod.DAY, DAY, DigestState.NOTHING_NEW)
    await digests_store.record(conn, written, None)
    row = await digests_store.get(conn, digest_id)
    assert (row["state"], row["acknowledged_at"]) == ("nothing_new", None)
    await fail_digest(conn, DAY + timedelta(days=1))
    assert (await status(client))["digests_failed"] == 1


async def test_a_failed_nightly_run_is_not_acknowledged(client, conn):
    await stop(conn, P, "analysis")
    await conn.execute(
        "INSERT INTO runs (kind, started_at, finished_at, error) "
        "VALUES ('nightly', now(), now(), 'archive down')"
    )
    await ack(client, await shown(client, "analysis_failed"))
    failed = await status(client)
    assert (failed["state"], failed["error"], failed["analysis_failed"]) == (
        "failed",
        "archive down",
        0,
    )


async def test_a_new_text_or_a_text_that_arrives_brings_a_failure_back(client, conn):
    """No owner's retry in between: a rewrite fails classification afresh, and a pass that
    waited for its text over a week gets it and then fails three runs."""
    await stop(conn, P, "classification")
    pass_id = await stop_pass(conn)
    await conn.execute(
        "UPDATE fulltext_passes SET state = 'awaiting_text', failed_runs = 0, "
        "queued_at = now() - interval '8 days' WHERE id = %s",
        (pass_id,),
    )
    await ack(client, await shown(client, "awaiting_classification", "fulltext"))
    assert (await status(client))["state"] == "quiet"

    rewritten = paper(P.id, title=P.title, abstract="A wholly new abstract.", version=2)
    await papers_store.upsert(conn, [rewritten])
    await stop(conn, rewritten.model_copy(update={"revision": 2}), "classification")
    await fail_pass(conn, pass_id)
    back = await status(client)
    assert (back["awaiting_classification"], back["fulltext_failed"]) == (1, 1)
