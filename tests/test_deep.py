"""Deep card passes: which papers get one and when (the owner's request, Unsave and Area picks
included), working one against the archive and a fake deep service, the usage each attempt
records, the lease and a worker that dies mid-job, the daily cap, revisions, and what the API
shows."""

import asyncio
import contextlib

import httpx
import pytest

from litwatch import events, store
from litwatch.api import create_app
from litwatch.archive import ArchiveClient
from litwatch.config import Config
from litwatch.core import (
    FAILED_RUNS,
    Cause,
    Fit,
    Hand,
    Label,
    LabelOrigin,
    LabelValue,
    Status,
    Verdict,
)
from litwatch.core.fulltext import (
    FOCUS,
    PICK,
    RECONCILE_LOOKBACK,
    REQUESTED,
    DeepPass,
    PassState,
    deep_to_drop,
    deep_to_queue,
    deep_to_relabel,
    deep_wanted,
)
from litwatch.feedback import misses, word
from litwatch.fulltext import deep as deep_passes
from litwatch.intake import search
from litwatch.integrations.deep import Deep, DeepClient
from litwatch.jobs import nightly
from litwatch.jobs.context import model_run
from litwatch.store import feedback as feedback_store
from litwatch.store import fulltext as fulltext_store
from litwatch.store import integrations as cards
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts
from tests.conftest import AGENTS, FAITH, INTERP, TREE, answering, paper
from tests.fakes_http import FakeArchive, FakeDeep, mock
from tests.test_digests import MON, TUE, Writer, area, day, listed, week
from tests.test_reading import analyse, classify
from tests.test_reading import grade as verdict

P = paper("2610.00001", title="Traces that hide steps", abstract="Hidden computation.", version=2)
Q = paper("2610.00002", title="Probes for hidden steps", abstract="Linear probes.", version=1)
BODY = "\\section{Method} We read hidden steps off the residual stream. " * 40
USAGE = {"turns": 7, "input": 41000, "output": 2300, "list_cost": 0.42, "cells": 4}
CARD = {
    "summary": "S.",
    "key_claim": "K.",
    "among_collection": [
        {"arxiv_id": Q.id, "title": "Probes", "relation": "extends", "note": "Same probes."},
        {"arxiv_id": "2501.00009", "title": "Elsewhere", "relation": "agrees"},
        {"arxiv_id": "2501.00010", "title": "Unknown kind", "relation": "cites"},
    ],
}
DONE = {"result": CARD, "stats": USAGE, "model": "deep-model", "trace": []}
TOKEN = "t"


def make(fake: FakeDeep, **kw) -> Deep:
    client = DeepClient("http://deep.test", http=mock(fake.handle), poll_seconds=0)
    return Deep(client, **{"max_per_day": 10, **kw})


def archive(fake: FakeArchive) -> ArchiveClient:
    return ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))


def with_text(*papers) -> FakeArchive:
    fake = FakeArchive()
    for p in papers:
        fake.texts[p.id] = (p.version, BODY)
    return fake


@pytest.fixture
async def db(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P, Q])
    return conn


async def grade(conn, fit: Fit, pid: str = P.id) -> None:
    """A Focus verdict; with no Weight yet (solid), central is must read."""
    await verdicts.add(
        conn,
        Verdict(
            paper_id=pid,
            topic=FAITH.slug,
            topic_version="v",
            fit=fit,
            reason="r",
            opinions={},
        ),
    )
    await word.settle(conn, pid, cause=Cause.VERDICT)


async def deep_rows(conn, pid: str = P.id) -> list[dict]:
    return await (
        await conn.execute(
            "SELECT * FROM fulltext_passes WHERE paper_id = %s AND kind = 'deep' ORDER BY id",
            (pid,),
        )
    ).fetchall()


async def attempts(conn) -> list[dict]:
    return await (await conn.execute("SELECT * FROM deep_attempts ORDER BY id")).fetchall()


async def work(conn, fake: FakeArchive, d: Deep, **kw):
    return await deep_passes.work(conn, archive(fake), d, **kw)


async def outbox_of(conn, kind: str) -> list[dict]:
    rows = await (
        await conn.execute("SELECT paper_id, payload FROM outbox WHERE type = %s", (kind,))
    ).fetchall()
    return [{"paper": r["paper_id"], **r["payload"]} for r in rows]


def test_an_automatic_card_needs_a_must_read_or_a_pick_not_unsaved_and_a_request_stays():
    # deep_wanted(focus_must_read, picked, unsaved, has_card)
    assert deep_wanted(True, False, False, False) == FOCUS
    assert deep_wanted(False, True, False, False) == PICK
    assert deep_wanted(True, True, False, False) == FOCUS
    assert deep_wanted(False, False, False, False) is None
    assert deep_wanted(True, True, True, False) is None  # unsaved
    assert deep_wanted(True, True, False, True) is None  # remade only on request
    automatic = DeepPass(1, FOCUS, PassState.AWAITING_TEXT)
    assert deep_to_drop(None, automatic) and not deep_to_queue(FOCUS, automatic)
    assert not deep_to_drop(None, DeepPass(1, REQUESTED, PassState.FAILED))
    assert not deep_to_drop(None, DeepPass(1, FOCUS, PassState.RUNNING))  # started
    # One wanted for the other automatic reason now says so; a request or a started one stays.
    assert deep_to_relabel(PICK, automatic) and not deep_to_relabel(FOCUS, automatic)
    assert not deep_to_relabel(PICK, DeepPass(1, REQUESTED, PassState.READY))
    assert not deep_to_relabel(PICK, DeepPass(1, FOCUS, PassState.RUNNING))


# -- which papers get one --------------------------------------------------------------------------


async def test_a_saved_focus_must_read_gets_one_deep_pass_and_unsave_or_a_pause_cancels_it(db):
    await grade(db, Fit.RELATED)  # relevant: no card
    assert await deep_rows(db) == []
    await grade(db, Fit.CENTRAL)  # must read, and so saved
    (row,) = await deep_rows(db)
    assert (row["reason"], row["state"], row["priority"]) == ("focus", "awaiting_text", 1)
    await word.settle(db, P.id, cause=Cause.VERDICT)
    assert len(await deep_rows(db)) == 1  # one open per paper

    await word.toggle(db, P.id, Hand.UNSAVE)
    assert await deep_rows(db) == []  # not started: cancelled
    await word.toggle(db, P.id, Hand.SAVE)
    assert len(await deep_rows(db)) == 1

    tree = await taxonomy_store.load(db)
    faith = next(t for t in tree.topics if t.slug == FAITH.slug)
    await word.save_taxonomy(db, tree.following(faith.model_copy(update={"status": Status.PAUSED})))
    assert await deep_rows(db) == []


async def test_the_owners_request_takes_over_an_automatic_pass_and_no_unsave_cancels_it(db):
    await grade(db, Fit.CENTRAL)
    (automatic,) = await deep_rows(db)
    assert await deep_passes.request(db, P.id) == automatic["id"]
    (row,) = await deep_rows(db)
    assert (row["reason"], row["priority"]) == ("request", 0)
    await word.toggle(db, P.id, Hand.UNSAVE)
    assert [r["id"] for r in await deep_rows(db)] == [automatic["id"]]

    # A paper graded nowhere gets its pass too, and the request is an event.
    other = await deep_passes.request(db, Q.id)
    assert [r["reason"] for r in await deep_rows(db, Q.id)] == ["request"]
    assert await outbox_of(db, events.CARD_REQUESTED) == [
        {"paper": P.id, "pass_id": automatic["id"]},
        {"paper": Q.id, "pass_id": other},
    ]
    await db.execute("UPDATE papers SET dismissed_at = now() WHERE id = %s", (Q.id,))
    assert await deep_passes.request(db, Q.id) is None


async def test_a_must_read_whose_settle_was_missed_gets_its_pass_on_the_nightly_sweep(db):
    await verdicts.add(
        db,
        Verdict(
            paper_id=P.id,
            topic=FAITH.slug,
            topic_version="v",
            fit=Fit.CENTRAL,
            reason="r",
            opinions={},
        ),
    )  # and no settle
    assert await deep_rows(db) == []
    await word.settle_all(
        db, await fulltext_store.candidates(db, RECONCILE_LOOKBACK), cause=Cause.VERDICT
    )
    assert [r["reason"] for r in await deep_rows(db)] == ["focus"]


async def test_a_missed_settle_is_swept_up_though_the_paper_has_its_brief(db):
    await grade(db, Fit.RELATED)  # relevant, with its Brief pass, done
    await db.execute(
        "UPDATE fulltext_passes SET state = 'done', arxiv_version = 2 WHERE kind = 'brief'"
    )
    await verdicts.add(
        db,
        Verdict(
            paper_id=P.id,
            topic=FAITH.slug,
            topic_version="v",
            fit=Fit.CENTRAL,
            reason="r",
            opinions={},
        ),
    )  # and no settle
    await word.settle_all(
        db, await fulltext_store.candidates(db, RECONCILE_LOOKBACK), cause=Cause.VERDICT
    )
    assert [r["reason"] for r in await deep_rows(db)] == ["focus"]


async def test_a_missed_settle_of_an_unsure_label_is_swept_up(db):
    await grade(db, Fit.CENTRAL)  # must read, with its Deep pass and its Brief pass, done
    await db.execute(
        "UPDATE fulltext_passes SET state = 'done', arxiv_version = 2 WHERE kind = 'brief'"
    )
    await db.execute("UPDATE verdicts SET recorded_at = now() - interval '60 days'")
    said = Label(
        paper_id=P.id, topic=FAITH.slug, value=LabelValue.RELATED, origin=LabelOrigin.READER
    )
    await word.label(db, said)  # relevant: the pass is dropped
    assert await deep_rows(db) == []
    unsure = said.model_copy(update={"value": LabelValue.UNSURE})
    await feedback_store.set_label(db, unsure)  # the central verdict counts again; no settle
    await word.settle_all(
        db, await fulltext_store.candidates(db, RECONCILE_LOOKBACK), cause=Cause.VERDICT
    )
    assert [r["reason"] for r in await deep_rows(db)] == ["focus"]


# -- working one -----------------------------------------------------------------------------------


async def test_a_deep_pass_awaits_text_then_writes_the_card_with_its_usage(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    d = make(fake)
    assert await work(db, FakeArchive(status="pending"), d) == {"awaiting_text": 1}
    assert fake.requests == []

    assert await work(db, with_text(P), d) == {"cards_written": 1}
    (sent,) = fake.requests
    assert sent["paper"]["body"] == BODY and sent["paper"]["body_source"] == "latex"
    assert sent["extensions"] == ["faithfulness"]
    assert [s["name"] for s in sent["selections"]] == ["Chain-of-thought faithfulness"]
    stored = await cards.card(db, P.id)
    assert (stored["card"], stored["model"]) == (CARD, "deep-model")
    assert (stored["revision"], stored["arxiv_version"], stored["source"]) == (1, 2, "latex")
    (attempt,) = await attempts(db)
    assert attempt["usage"] == USAGE and attempt["ended_at"] is not None and not attempt["error"]
    assert attempt["automatic"] and attempt["job_id"] == "job-1"
    (row,) = await deep_rows(db)
    assert (row["state"], row["arxiv_version"], row["text_source"]) == ("done", 2, "latex")
    ready = await outbox_of(db, events.CARD_READY)
    assert ready == [{"paper": P.id, "model": "deep-model", "pass_id": row["id"]}]

    await word.settle(db, P.id, cause=Cause.VERDICT)  # a card is remade only on request
    assert len(await deep_rows(db)) == 1


async def test_a_failed_job_records_its_usage_and_waits_for_the_owner_after_three_runs(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep({"error": "cell budget exhausted", "stats": USAGE, "trace": []})
    d = make(fake)
    for run in range(FAILED_RUNS):
        assert await fulltext_store.retry_failed(db, FAILED_RUNS) == (1 if run else 0)
        assert await work(db, with_text(P), d) == {"card_failed": 1}
    assert await fulltext_store.retry_failed(db, FAILED_RUNS) == 0  # no longer tried
    assert [(a["usage"], a["error"]) for a in await attempts(db)] == [
        (USAGE, "cell budget exhausted")
    ] * FAILED_RUNS
    (row,) = await deep_rows(db)
    assert (row["state"], row["failed_runs"]) == ("failed", FAILED_RUNS)
    assert (await cards.card(db, P.id))["error"] == "cell budget exhausted"
    (stuck,) = await fulltext_store.stuck(db, FAILED_RUNS)
    assert (stuck["id"], stuck["kind"]) == (row["id"], "deep")
    assert await fulltext_store.run_now(db, row["id"], FAILED_RUNS)
    (row,) = await deep_rows(db)  # the owner's now: first in line, past the daily cap
    assert (row["state"], row["reason"], row["priority"]) == ("ready", "request", 0)


async def test_the_owners_request_tries_a_failed_pass_again(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep({"error": "cell budget exhausted", "stats": USAGE, "trace": []})
    d = make(fake)
    for _ in range(FAILED_RUNS):
        await fulltext_store.retry_failed(db, FAILED_RUNS)
        assert await work(db, with_text(P), d) == {"card_failed": 1}
    (failed,) = await deep_rows(db)
    fake.outcome = DONE
    assert await deep_passes.request(db, P.id) == failed["id"]
    assert await work(db, with_text(P), d) == {"cards_written": 1}


async def test_a_lost_job_or_an_answer_that_never_came_has_its_usage_unknown(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.vanish = True
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_failed": 1}
    fake.vanish, fake.lose_answers = False, 1
    await fulltext_store.retry_failed(db, FAILED_RUNS)
    assert await work(db, with_text(P), d) == {"card_failed": 1}
    found = await attempts(db)
    assert [(a["usage"], a["ended_at"] is not None) for a in found] == [(None, True)] * 2
    assert "vanished" in found[0]["error"] and "submitting failed" in found[1]["error"]

    await fulltext_store.retry_failed(db, FAILED_RUNS)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    month = await cards.usage_since(db, found[0]["at"])
    assert month == {"attempts": 3, "list_cost": USAGE["list_cost"], "unknown": 2}


async def test_a_job_left_by_a_dead_worker_is_followed_not_paid_for_twice(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.hold = True
    d = make(fake)
    task = asyncio.create_task(work(db, with_text(P), d))
    while not fake.polls:
        await asyncio.sleep(0.01)
    task.cancel()  # the worker dies while its job runs
    with contextlib.suppress(asyncio.CancelledError):
        await task
    (row,) = await deep_rows(db)
    assert row["state"] == "running"
    # Its lease runs out; meanwhile the paper is unsaved, which a started card ignores.
    await db.execute("UPDATE fulltext_passes SET lease_until = now() - interval '1 second'")
    await word.toggle(db, P.id, Hand.UNSAVE)
    fake.hold = False
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    assert len(fake.requests) == 1 and len(await attempts(db)) == 1


async def test_a_failed_poll_lets_the_pass_go_and_its_next_claim_follows_the_job(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.fail_polls = 1
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_following": 1}
    (row,) = await deep_rows(db)
    assert row["state"] == "running" and "following the deep job" in row["last_error"]
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    assert len(fake.requests) == 1


async def test_a_submitted_job_is_followed_past_the_daily_cap_and_a_dismissal(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.fail_polls = 1
    d = make(fake, max_per_day=1)
    assert await work(db, with_text(P), d) == {"card_following": 1}
    assert await work(db, with_text(P), d) == {"cards_written": 1}  # the cap is spent

    await deep_passes.request(db, Q.id)
    fake.fail_polls = 1
    assert await work(db, with_text(Q), d) == {"card_following": 1}
    await db.execute("UPDATE papers SET dismissed_at = now() WHERE id = %s", (Q.id,))
    assert await work(db, with_text(Q), d) == {"cards_written": 1}
    assert [(a["usage"], a["ended_at"] is not None) for a in await attempts(db)] == [
        (USAGE, True)
    ] * 2
    assert len(fake.requests) == 2


async def test_a_request_that_takes_over_a_claimed_pass_makes_the_attempt_the_owners(
    db, monkeypatch
):
    await grade(db, Fit.CENTRAL)
    reading = deep_passes.read

    async def read_then_request(archive, c):
        found = await reading(archive, c)
        await deep_passes.request(db, P.id)  # while the worker reads the text
        return found

    monkeypatch.setattr(deep_passes, "read", read_then_request)
    d = make(FakeDeep(DONE), max_per_day=1)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    assert [a["automatic"] for a in await attempts(db)] == [False]


async def test_a_paper_dismissed_or_unsaved_during_the_read_is_never_submitted(db, monkeypatch):
    reading = deep_passes.read
    meanwhile = []

    async def read_then(archive, c):
        found = await reading(archive, c)
        await meanwhile.pop(0)()  # while the worker reads the text
        return found

    async def dismiss():
        await db.execute("UPDATE papers SET dismissed_at = now() WHERE id = %s", (P.id,))

    async def unsave():
        await word.toggle(db, P.id, Hand.UNSAVE)

    monkeypatch.setattr(deep_passes, "read", read_then)
    await deep_passes.request(db, P.id)
    fake = FakeDeep(DONE)
    meanwhile.append(dismiss)
    assert await work(db, with_text(P), make(fake)) == {"fulltext_dismissed": 1}

    await db.execute("UPDATE papers SET dismissed_at = NULL")
    await db.execute("DELETE FROM fulltext_passes")
    await grade(db, Fit.CENTRAL)  # saved, a Focus must read: an automatic pass
    meanwhile.append(unsave)
    assert await work(db, with_text(P), make(fake)) == {"fulltext_dropped": 1}
    assert (fake.requests, await attempts(db)) == ([], [])


async def test_a_job_whose_polls_keep_failing_is_given_up_at_its_deadline(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.fail_polls = 1
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_following": 1}
    await db.execute("UPDATE deep_attempts SET at = now() - interval '2 hours'")
    fake.fail_polls = 100  # the service is gone for good
    assert await work(db, with_text(P), d) == {"card_failed": 1}
    ((usage, error),) = [(a["usage"], a["error"]) for a in await attempts(db)]
    assert usage is None and "gave up" in error


async def test_one_worker_at_a_time_works_the_deep_passes(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    other = await store.connect(db.test_dsn, db.test_schema)
    async with other, fulltext_store.deep_worker(other) as mine:
        assert mine
        assert await work(db, with_text(P), make(fake)) == {}
    assert fake.requests == [] and [r["state"] for r in await deep_rows(db)] == ["awaiting_text"]
    assert await work(db, with_text(P), make(fake)) == {"cards_written": 1}


async def test_a_pass_no_longer_wanted_at_claim_spends_nothing(db):
    await grade(db, Fit.CENTRAL)
    # Unsaved by a write that missed its settle: the claim settles it first.
    await db.execute(
        "UPDATE library SET hand = 'unsave', saved = false WHERE paper_id = %s", (P.id,)
    )
    fake = FakeDeep(DONE)
    assert await work(db, with_text(P), make(fake)) == {"fulltext_dropped": 1}
    assert fake.requests == [] and await deep_rows(db) == []


async def test_the_daily_cap_holds_back_automatic_cards_and_never_the_owners_request(db):
    await grade(db, Fit.CENTRAL)
    await grade(db, Fit.CENTRAL, Q.id)
    fake = FakeDeep(DONE)
    d = make(fake, max_per_day=1)
    assert await work(db, with_text(P, Q), d) == {"cards_written": 1}
    (waiting,) = [
        r for r in [*await deep_rows(db), *await deep_rows(db, Q.id)] if r["state"] != "done"
    ]
    assert waiting["tried_at"] is None  # not claimed, past the cap
    await deep_passes.request(db, waiting["paper_id"])
    assert await work(db, with_text(P, Q), d) == {"cards_written": 1}
    assert [a["automatic"] for a in await attempts(db)] == [True, False]


async def test_a_card_says_the_revision_it_read_and_is_remade_only_on_request(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    d = make(fake)
    await work(db, with_text(P), d)
    rewrite = P.model_copy(update={"version": 3, "title": "Entirely different words now"})
    await word.settle_all(
        db, (await papers_store.upsert(db, [rewrite])).rewritten, cause=Cause.VERDICT
    )
    assert [r["state"] for r in await deep_rows(db)] == ["done"]  # nothing new by itself
    assert (await cards.card(db, P.id))["revision"] == 1

    await deep_passes.request(db, P.id)
    assert await work(db, with_text(rewrite), d) == {"cards_written": 1}
    stored = await cards.card(db, P.id)
    assert (stored["revision"], stored["arxiv_version"]) == (2, 3)


# -- Area picks ------------------------------------------------------------------------------------

R = paper("2610.00003", title="Tools that plan", abstract="Agents.", version=1)


async def must_read_in_the_area(conn, p=P) -> None:
    """Graded must read in the Area from its abstract, its Weight re-check done: a pick
    candidate."""
    await area(conn, p, "central")
    await conn.execute(
        "UPDATE fulltext_passes SET state = 'done', arxiv_version = %s "
        "WHERE paper_id = %s AND kind = 'weight'",
        (p.version, p.id),
    )


async def picked(conn) -> None:
    """P picked in the Area's published daily digest of Tuesday."""
    await must_read_in_the_area(conn)
    await listed(conn, TUE, P)
    await day(conn, Writer(), TUE)


async def roles(conn, topic: str, period: str) -> dict[str, str]:
    rows = await (
        await conn.execute(
            "SELECT dp.paper_id, dp.role FROM digest_papers dp "
            "JOIN digests d ON d.id = dp.digest_id WHERE d.topic = %s AND d.period = %s",
            (topic, period),
        )
    ).fetchall()
    return {r["paper_id"]: r["role"] for r in rows}


async def test_an_area_pick_gets_its_deep_card_when_its_digest_is_published_and_is_not_saved(db):
    await must_read_in_the_area(db)
    await area(db, Q, "related")  # relevant: listed, not a pick candidate
    await listed(db, TUE, P, Q)
    assert await deep_rows(db) == []  # a must read in an Area alone gets no card
    await day(db, Writer(), TUE)
    assert await roles(db, INTERP.slug, "day") == {P.id: "pick", Q.id: "listed"}
    (row,) = await deep_rows(db)
    assert (row["reason"], row["state"], row["priority"]) == ("pick", "awaiting_text", 1)
    assert await deep_rows(db, Q.id) == []
    # Picks are not saved (decision 3).
    saved = await (
        await db.execute("SELECT saved FROM library WHERE paper_id = %s", (P.id,))
    ).fetchone()
    assert not (saved and saved["saved"]) and await outbox_of(db, events.PAPER_SAVED) == []

    # Automatic: the daily cap holds it back, and the paper page shows why it is queued.
    _, http = client(db, [make(FakeDeep(DONE), max_per_day=0)])
    async with http:
        page = (await http.get(f"/api/papers/{P.id}")).json()
    assert page["card_pass"]["reason"] == "pick"
    fake = FakeDeep(DONE)
    assert await work(db, with_text(P), make(fake, max_per_day=0)) == {}
    assert await work(db, with_text(P), make(fake)) == {"cards_written": 1}
    (sent,) = fake.requests
    assert [s["name"] for s in sent["selections"]] == ["Interpretability"]
    assert [a["automatic"] for a in await attempts(db)] == [True]
    await word.settle(db, P.id, cause=Cause.VERDICT)  # a card already made is not made again
    assert [r["state"] for r in await deep_rows(db)] == ["done"]


async def test_weekly_area_picks_get_cards_and_field_picks_do_not(db):
    """Q, only listed in Tuesday's daily digest, is picked in the Area's weekly one; R, picked
    in the Field's weekly digest, is not an Area pick."""
    await papers_store.upsert(db, [R])
    await must_read_in_the_area(db)
    await area(db, Q, "related")
    await classify(db, R, AGENTS.slug)
    await analyse(db, R)
    await verdict(db, R, "related", AGENTS)
    await verdicts.passed(db, [(R.id, 1)])
    await listed(db, TUE, P, Q, R)
    w = Writer()
    await day(db, w, TUE)
    assert await deep_rows(db, Q.id) == []
    await week(db, w, MON)
    assert (await roles(db, INTERP.slug, "week"))[Q.id] == "pick"
    assert await roles(db, AGENTS.slug, "week") == {R.id: "pick"}
    assert [r["reason"] for r in await deep_rows(db, Q.id)] == ["pick"]
    assert await deep_rows(db, R.id) == []


@pytest.mark.parametrize(
    "missed",
    [
        "UPDATE digests SET state = 'failed', text = NULL, gist = NULL",
        "UPDATE topics SET status = 'paused' WHERE class = 'interpretability'",
    ],
    ids=["digest-not-published", "area-paused"],
)
async def test_a_pick_pass_is_judged_again_at_claim_and_spends_nothing_unwanted(db, missed):
    await picked(db)
    await db.execute(missed)  # with no settle
    fake = FakeDeep(DONE)
    assert await work(db, with_text(P), make(fake)) == {"fulltext_dropped": 1}
    assert fake.requests == [] and await deep_rows(db) == []


async def test_an_unsave_a_pause_or_a_rewrite_drops_a_pick_pass_not_started(db):
    await picked(db)
    await word.toggle(db, P.id, Hand.UNSAVE)
    assert await deep_rows(db) == []
    assert P.id not in await fulltext_store.candidates(db, RECONCILE_LOOKBACK)
    await word.toggle(db, P.id, Hand.SAVE)  # the owner's Save: the pick wants its card again
    assert [r["reason"] for r in await deep_rows(db)] == ["pick"]

    tree = await taxonomy_store.load(db)
    interp = next(t for t in tree.topics if t.slug == INTERP.slug)
    paused = tree.following(interp.model_copy(update={"status": Status.PAUSED}))
    await word.save_taxonomy(db, paused)
    assert await deep_rows(db) == []
    await word.save_taxonomy(db, paused.following(interp))
    assert [r["reason"] for r in await deep_rows(db)] == ["pick"]

    # A rewrite: the pick judged text that no longer exists.
    rewrite = P.model_copy(update={"version": 3, "title": "Entirely different words now"})
    await word.settle_all(
        db, (await papers_store.upsert(db, [rewrite])).rewritten, cause=Cause.VERDICT
    )
    assert await deep_rows(db) == []


async def test_a_dismissed_pick_is_never_worked(db):
    await picked(db)
    assert [r["reason"] for r in await deep_rows(db)] == ["pick"]
    await papers_store.dismiss(db, P.id)
    fake = FakeDeep(DONE)
    assert await work(db, with_text(P), make(fake)) == {}
    assert fake.requests == []
    assert P.id not in await fulltext_store.candidates(db, RECONCILE_LOOKBACK)


async def test_a_pick_pass_once_for_a_focus_must_read_says_it_is_for_the_pick(db):
    await picked(db)
    await db.execute("UPDATE fulltext_passes SET reason = 'focus' WHERE kind = 'deep'")
    await word.settle(db, P.id, cause=Cause.VERDICT)
    assert [r["reason"] for r in await deep_rows(db)] == ["pick"]


async def test_a_pick_whose_settle_was_missed_is_swept_up_whatever_the_limit(db):
    await picked(db)
    await db.execute("DELETE FROM fulltext_passes WHERE kind = 'deep'")  # its settle missed
    assert P.id in await fulltext_store.candidates(db, RECONCILE_LOOKBACK)
    async with model_run(db, Config(), "nightly", gateway=Writer().gateway) as run:
        await nightly.text_passes(db, Config(), run, limit=0)  # no archive: it only reconciles
    assert [r["reason"] for r in await deep_rows(db)] == ["pick"]

    await work(db, with_text(P), make(FakeDeep(DONE)))
    assert P.id not in await fulltext_store.candidates(db, RECONCILE_LOOKBACK)  # it has its card


# -- what the owner sees ---------------------------------------------------------------------------


def client(conn, integrations) -> tuple:
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, Config(), integrations=integrations)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    return app, http


async def test_the_owner_may_ask_for_a_card_an_automatic_pass_waits_for(db):
    """Held back by the daily cap, the automatic pass is open; asking takes it over."""
    await grade(db, Fit.CENTRAL)
    _, http = client(db, [make(FakeDeep(DONE), max_per_day=0)])
    async with http:
        page = (await http.get(f"/api/papers/{P.id}")).json()
        asked = await http.post(f"/api/papers/{P.id}/card")
        after = (await http.get(f"/api/papers/{P.id}")).json()
    assert (page["card_pass"]["reason"], page["card_requestable"]) == ("focus", True)
    assert asked.status_code == 202 and asked.json()["pass_id"] == page["card_pass"]["id"]
    assert (after["card_pass"]["reason"], after["card_requestable"]) == ("request", False)


async def test_deep_read_on_a_paper_litwatch_lacks_fetches_it_first(db, monkeypatch):
    """Deep read outside the collection: an archive paper not fetched yet is fetched as
    Process fetches it, then its card is asked for; arXiv lacking it is a 404."""
    fetched = []
    found = {"2610.09999": paper("2610.09999", title="Elsewhere")}
    monkeypatch.setattr(
        misses,
        "fetch_by_ids",
        answering(lambda ids: fetched.append(ids) or [found[i] for i in ids if i in found]),
    )
    _, http = client(db, [make(FakeDeep(DONE))])
    async with http:
        asked = await http.post("/api/papers/2610.09999v2/card")
        page = (await http.get("/api/papers/2610.09999")).json()
        missing = await http.post("/api/papers/2601.99999/card")
        # A request that fails after the fetch keeps nothing of it.
        found["2610.08888"] = paper("2610.08888")

        async def failing(*_):
            raise RuntimeError("no pass")

        with monkeypatch.context() as m, pytest.raises(RuntimeError):
            m.setattr(deep_passes, "request", failing)
            await http.post("/api/papers/2610.08888/card")
    assert asked.status_code == 202 and fetched[:2] == [["2610.09999"], ["2601.99999"]]
    assert (page["stored"], page["in_collection"]) == (True, False)
    assert page["card_pass"]["reason"] == "request" and missing.status_code == 404
    assert await papers_store.get(db, "2610.08888") is None


async def test_the_paper_page_shows_the_request_queued_then_the_card_and_write_again(
    db, monkeypatch, slow_arxiv
):
    await db.execute(
        "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
        "primary_class, model) VALUES (%s, 1, 'x', 'interpretability', 'm')",
        (Q.id,),
    )
    await word.settle(db, Q.id, cause=Cause.VERDICT)  # in the collection now, with a rarity
    _, bare = client(db, [])
    async with bare:
        page = (await bare.get(f"/api/papers/{P.id}")).json()
        refused = await bare.post(f"/api/papers/{P.id}/card")
    assert (page["card_pass"], page["card_requestable"], refused.status_code) == (None, False, 409)

    fake = FakeDeep(DONE)
    d = make(fake)
    app, http = client(db, [d])
    monkeypatch.setattr(misses, "fetch_by_ids", answering(lambda ids: []))  # arXiv lacks it too
    async with http:
        missing = await http.post("/api/papers/2601.99999/card")
        # arXiv trickling its answer in: the request ends within its budget, with a 503 (P5).
        with monkeypatch.context() as m:
            m.setattr(search, "LOOKUP_BUDGET", 0.3)
            m.setattr(misses, "fetch_by_ids", search.fetch_by_ids)
            m.delenv("ARXIV_PACING_DIR", raising=False)
            silent = await asyncio.wait_for(http.post("/api/papers/2601.99998/card"), 5)
        assert silent.status_code == 503 and silent.json()["detail"] == misses.SILENT
        asked = await http.post(f"/api/papers/{P.id}/card")
        queued = (await http.get(f"/api/papers/{P.id}")).json()
        await work(db, with_text(P), d)
        written = (await http.get(f"/api/papers/{P.id}")).json()
        again = await http.post(f"/api/papers/{P.id}/card")
        quality = (await http.get("/api/settings/quality")).json()
    assert missing.status_code == 404
    assert asked.status_code == 202 and app.state.deep_wake.is_set()
    assert queued["card_pass"]["reason"] == "request" and not queued["card_requestable"]
    assert queued["card_pass"]["state"] == "awaiting_text" and queued["card"] is None
    card = written["card"]
    assert (card["revision"], card["arxiv_version"], card["source"]) == (1, 2, "latex")
    # The rows link by arXiv id; one of the collection's has its rarity, a relation the
    # section does not know is left out.
    assert [(r["id"], r["relation"], r["in_collection"]) for r in card["among"]] == [
        (Q.id, "extends", True),
        ("2501.00009", "agrees", False),
    ]
    assert card["among"][0]["title"] == Q.title and card["among"][0]["rarity"] is not None
    assert written["card_pass"] is None and written["card_requestable"]
    assert again.status_code == 202 and again.json()["pass_id"] != asked.json()["pass_id"]
    assert quality["deep"] == {"attempts": 1, "list_cost": USAGE["list_cost"], "unknown": 0}


async def test_the_deep_worker_runs_a_requested_card_as_soon_as_it_is_woken(db):
    fake = FakeDeep(DONE)
    d = make(fake)
    wake = asyncio.Event()
    dsn = f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"
    worker = asyncio.create_task(deep_passes.run_forever(dsn, archive(with_text(P)), d, wake))
    try:
        await deep_passes.request(db, P.id)
        wake.set()
        for _ in range(200):
            if (await cards.card(db, P.id) or {}).get("card"):
                break
            await asyncio.sleep(0.05)
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
    assert (await cards.card(db, P.id))["card"] == CARD


async def test_connect_reaches_the_test_schema(db):
    async with await store.connect(f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"):
        pass
