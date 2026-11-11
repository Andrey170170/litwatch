"""Deep card passes: which papers get one and when (the owner's request, Unsave and Area picks
included), working one against the archive and a fake deep service, the usage each attempt
records, the lease and a worker that dies mid-job, the service's failures and the paper's, no
daily cap, revisions, and what the API shows. A Deep read: its format tag, its figures and
their endpoint, its reviews in the trace, its `review` note on Needs you."""

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from litwatch import events, store
from litwatch.api import create_app, monitor
from litwatch.archive import ArchiveClient
from litwatch.config import Config, Schedule
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
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts
from tests.conftest import (
    AGENTS,
    BLUE,
    FAITH,
    INTERP,
    RUST,
    TREE,
    answering,
    fixture_read,
    paper,
    sent_figure,
)
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
    return Deep(client, **kw)


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


async def unpause(conn) -> None:
    """As if the queue's pause after the last service failure were over."""
    await conn.execute(
        "UPDATE deep_attempts SET service_failed_at = service_failed_at - %s",
        (deep_passes.PAUSE,),
    )


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
    assert sent["paper"]["body_version"] == 2  # the text's arXiv version, for its figures
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


async def field(conn, p, fit: str) -> None:
    """A member of the Field topic's class, graded there at `fit`, grading finished."""
    await classify(conn, p, AGENTS.slug)
    await analyse(conn, p)
    await verdict(conn, p, fit, AGENTS)
    await verdicts.passed(conn, [(p.id, 1)])


@pytest.mark.parametrize(
    ("graded", "selections"),
    [
        (None, []),
        (lambda c: verdict(c, Q, "none"), []),
        (lambda c: area(c, Q, "related"), [("interpretability", "Interpretability", "relevant")]),
        (lambda c: field(c, Q, "related"), [("agents", "Agents and tool use", "relevant")]),
    ],
    ids=["graded-nowhere", "graded-none", "area-not-picked", "field"],
)
async def test_a_requested_card_is_written_for_the_topics_that_graded_the_paper(
    db, graded, selections
):
    """Not for every Focus topic: a paper no topic graded gets no topic context, and one
    graded only outside the CoT topics gets no CoT extension."""
    if graded:
        await graded(db)
    await deep_passes.request(db, Q.id)
    fake = FakeDeep(DONE)
    assert await work(db, with_text(Q), make(fake)) == {"cards_written": 1}
    (sent,) = fake.requests
    # The slug is what the read's lens names its topic by (`Lens.topic`, deep/src/read.ts).
    assert [(s["slug"], s["name"], s["grade"]) for s in sent["selections"]] == selections
    assert sent["extensions"] == []


async def test_a_failed_job_records_its_usage_and_waits_for_the_owner_after_three_runs(db):
    await grade(db, Fit.CENTRAL)
    failed = {"error": "cell budget exhausted", "failure": "paper", "stats": USAGE, "trace": []}
    fake = FakeDeep(failed)
    d = make(fake)
    for run in range(FAILED_RUNS):
        assert await fulltext_store.retry_failed(db, FAILED_RUNS) == (1 if run else 0)
        assert await work(db, with_text(P), d) == {"card_failed": 1}
    assert await fulltext_store.retry_failed(db, FAILED_RUNS) == 0  # no longer tried
    assert [(a["usage"], a["error"], a["failure"]) for a in await attempts(db)] == [
        (USAGE, "cell budget exhausted", "paper")
    ] * FAILED_RUNS
    (row,) = await deep_rows(db)
    assert (row["state"], row["failed_runs"]) == ("failed", FAILED_RUNS)
    assert (await cards.card(db, P.id))["error"] == "cell budget exhausted"
    (stuck,) = await fulltext_store.stuck(db, FAILED_RUNS)
    assert (stuck["id"], stuck["kind"]) == (row["id"], "deep")
    assert await fulltext_store.run_now(db, row["id"], FAILED_RUNS)
    (row,) = await deep_rows(db)  # the owner's now: first in line
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


async def test_a_job_the_service_lost_has_its_usage_unknown_and_counts_against_nothing(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.vanish = True
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    (lost,) = await attempts(db)
    assert (lost["usage"], lost["failure"]) == (None, "service") and "vanished" in lost["error"]
    (row,) = await deep_rows(db)
    assert (row["state"], row["failed_runs"]) == ("ready", 0)  # back in the queue, uncounted

    fake.vanish = False
    await unpause(db)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    month = await cards.usage_since(db, lost["at"])
    assert month == {"attempts": 2, "list_cost": USAGE["list_cost"], "unknown": 1}


def trace(text: str) -> list[dict]:
    """A job's trace of one turn."""
    return [{"t": 0, "type": "assistant", "text": text, "thinking": "", "stop": "stop"}]


async def test_each_attempt_keeps_its_own_trace_and_a_job_without_one_keeps_none(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.vanish = True
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}  # lost: no trace
    await unpause(db)
    fake.vanish = False
    fake.outcome = {"error": "no valid card", "failure": "paper", "stats": USAGE}
    fake.outcome["trace"] = trace("failed")
    assert await work(db, with_text(P), d) == {"card_failed": 1}
    fake.outcome = DONE | {"trace": trace("carded")}
    await deep_passes.request(db, P.id)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    fake.answer_submits = (422, "no such extension")  # turned down: its job never ran
    await deep_passes.request(db, P.id)
    assert await work(db, with_text(P), d) == {"card_failed": 1}

    kept = [a["trace"] for a in await attempts(db)]
    assert kept == [None, trace("failed"), trace("carded"), None]
    assert "trace" not in await (await db.execute("SELECT * FROM deep_cards")).fetchone()


# What a P2 job's trace holds: a turn, a cell that timed out, a failed fetch that was redirected.
RICH = [
    {"t": 900, "type": "assistant", "text": "Reading.", "thinking": "Check the repo.",
     "stop": "toolUse", "usage": {"input": 4100, "output": 300, "reasoning": 120}},
    {"t": 1500, "type": "host", "name": "fetch", "args": '["https://anonymous.4open.science/r/x"]',
     "ms": 40, "ok": False, "chars": 0, "error": "10.0.0.1 is not a public address",
     "fetch": {"hops": [{"url": "https://anonymous.4open.science/api/repo/x/files/",
                         "status": 302}]}},
    {"t": 1600, "type": "host", "name": "fetch", "args": '["https://example.org/"]', "ms": 80,
     "ok": True, "chars": 812, "fetch": {"hops": [], "url": "https://example.org/",
                                          "status": 200, "bytes": 1024}},
    {"t": 92000, "type": "cell", "n": 1, "code": "while (true) {}", "output": "", "ms": 90001,
     "error": "cell ran longer than 90s", "timedOut": True},
    {"t": 93000, "type": "submit", "ok": True, "stood": ["Table 2", "0.00005"]},
]  # fmt: skip
NOTE = {"kind": "access", "source": "model", "note": "The repository could not be listed.",
        "url": "https://anonymous.4open.science/r/x"}  # fmt: skip


async def test_the_deep_commands_read_each_attempts_trace_and_the_cards_notes(
    db, tmp_path, monkeypatch
):
    from typer.testing import CliRunner  # noqa: PLC0415

    from litwatch import cli  # noqa: PLC0415

    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.vanish = True
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}  # lost: no trace
    await unpause(db)
    fake.vanish = False
    fake.outcome = DONE | {"result": CARD | {"process_notes": [NOTE]}, "trace": RICH}
    assert await work(db, with_text(P), d) == {"cards_written": 1}

    monkeypatch.chdir(tmp_path)  # every command loads ./.env
    monkeypatch.setenv(
        "LITWATCH_DATABASE_URL", f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"
    )
    config = tmp_path / "litwatch.toml"
    config.write_text('[schedule]\ntimezone = "UTC"\n')

    async def run(*args: str) -> str:
        argv = ["deep", *args, "--config", str(config)]
        result = await asyncio.to_thread(CliRunner().invoke, cli.app, argv)
        assert result.exit_code == 0, result.output
        return result.output

    latest = await run("trace", P.id)
    listed = latest.split("\n\n")[0].splitlines()[2:]
    assert [line.split()[0] for line in listed] == ["1", "2"]
    assert "Attempt 2 (id" in latest and "wrote the card" in latest
    assert "turn 1 · toolUse · in 4100 out 300 reasoning 120" in latest
    assert "reasoning: Check the repo." in latest
    assert "· failed: 10.0.0.1 is not a public address" in latest
    assert "went: 302 https://anonymous.4open.science/api/repo/x/files/ → no page" in latest
    assert "went: 200 https://example.org/ · 1024 bytes read" in latest
    assert "cell 1 · 90001 ms · timed out" in latest
    assert "submit · ok · stood by: Table 2; 0.00005" in latest
    assert "access (model): The repository could not be listed." in latest
    first = await run("trace", P.id, "--attempt", "1")
    assert "No trace was kept" in first and "failed (service)" in first
    assert "Process notes" not in first  # attempt 2 wrote the card, and its notes
    whole = json.loads(await run("trace", P.id, "--json"))
    assert whole["trace"] == RICH and whole["attempt"] == 2 and len(whole["attempts"]) == 2
    assert whole["process_notes"] == [NOTE]
    earlier = json.loads(await run("trace", P.id, "--attempt", "1", "--json"))
    assert (earlier["trace"], earlier["process_notes"]) == (None, [])

    notes = await run("notes")
    assert P.id in notes and "access (model): The repository could not be listed." in notes
    assert "<https://anonymous.4open.science/r/x>" in notes
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    assert await run("notes", "--since", tomorrow) == "No process notes.\n"


# -- a Deep read -----------------------------------------------------------------------------------

READ = fixture_read("scarce")
FIGURES = [
    sent_figure("methodology_by_step"),
    sent_figure("channel_util_s1", RUST),
    sent_figure("pact_vs_bid"),
]


def citing(read: dict, names: set[str]) -> dict:
    """The read with only the figures named, so what it cites is what is sent with it."""
    keep = lambda b: b["type"] != "fig" or b["name"] in names  # noqa: E731
    return read | {
        "sections": [
            s | {"blocks": [b for b in s["blocks"] if keep(b)]} if "blocks" in s else s
            for s in read["sections"]
        ]
    }


# Two review rounds as the service returns them (docs/integrations.md): the first sends the
# read back, the second passes it.
REVIEWS = [
    {
        "round": 1,
        "passed": False,
        "scores": {
            "followability": 3,
            "faithfulness": 4,
            "clarity": 3,
            "depth": 5,
            "calibration": 4,
        },
        "checks": {"anchors": True, "voice": False},
        "retelling": "A memory module helps; what it is tested on is unclear.",
        "findings": [
            {
                "about": "clarity",
                "where": "who/1",
                "text": "The sentence hides who ran the ablation.",
                "rewrite": "The authors ablate the memory module.",
            },
            {"about": "voice", "where": "glance", "text": "Says 'for you'."},
        ],
    },
    {
        "round": 2,
        "passed": True,
        "scores": {"faithfulness": 5, "clarity": 4, "depth": 5, "calibration": 4},
        "checks": {"anchors": True, "voice": True},
        "findings": [],
    },
]
WROTE_READ = DONE | {"result": READ, "figures": FIGURES, "reviews": REVIEWS}


async def figures(conn, pid: str = P.id) -> dict[str, tuple[str, bytes]]:
    rows = await (
        await conn.execute("SELECT name, mime, data FROM deep_figures WHERE paper_id = %s", (pid,))
    ).fetchall()
    return {r["name"]: (r["mime"], bytes(r["data"])) for r in rows}


async def test_a_read_is_stored_with_its_figures_and_reviews_and_a_new_one_replaces_them(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(WROTE_READ)
    d = make(fake)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    stored = await cards.card(db, P.id)
    assert stored["card"] == READ and stored["card"]["format"] == "read"
    assert await figures(db) == {
        "methodology_by_step": ("image/webp", BLUE),
        "channel_util_s1": ("image/webp", RUST),
        "pact_vs_bid": ("image/webp", BLUE),
    }
    (attempt,) = await attempts(db)
    assert attempt["reviews"] == REVIEWS and attempt["error"] is None
    assert await outbox_of(db, events.CARD_READY) == [
        {"paper": P.id, "model": "deep-model", "pass_id": attempt["pass_id"]}
    ]

    # Written again: the new read's figures are the paper's, the old ones gone with their read.
    fake.outcome = WROTE_READ | {
        "result": citing(READ, {"pact_vs_bid"}),
        "figures": [sent_figure("pact_vs_bid", RUST)],
        "reviews": [],
    }
    await deep_passes.request(db, P.id)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    assert await figures(db) == {"pact_vs_bid": ("image/webp", RUST)}
    assert [a["reviews"] for a in await attempts(db)] == [REVIEWS, []]

    # A Deep card, from a service that writes none yet: no format, no figures, no review ran.
    fake.outcome = DONE
    await deep_passes.request(db, P.id)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    assert (await cards.card(db, P.id))["card"] == CARD and await figures(db) == {}
    assert (await attempts(db))[-1]["reviews"] is None


@pytest.mark.parametrize(
    ("sent", "said"),
    [
        ({"result": READ | {"format": "essay"}}, "unknown format 'essay'"),
        ({"result": READ | {"format": None}}, "unknown format None"),
        ({"result": ["not", "a", "card"]}, "the result is not an object"),
        ({"figures": [sent_figure("f", mime="image/svg+xml")]}, "figure f is 'image/svg+xml'"),
        ({"figures": [sent_figure("f", b"<svg/>")]}, "figure f is not a WebP image"),
        ({"figures": [{"name": "f", "mime": "image/webp", "data": "%%%"}]}, "is not base64"),
        ({"figures": [sent_figure("../f")]}, "a figure's name is not one: '../f'"),
        ({"figures": [sent_figure("f"), sent_figure("f")]}, "two figures are named f"),
        ({"figures": {"f": "x"}}, "`figures` is not a list"),
        ({"reviews": "passed"}, "`reviews` is not a list"),
    ],
    ids=[
        "format", "null-format", "no-object", "mime", "not-webp", "base64", "name", "twice",
        "figures-shape", "reviews-shape",
    ],
)  # fmt: skip
async def test_a_result_litwatch_cannot_store_is_the_services_failure_and_stores_nothing(
    db, sent, said
):
    """An unknown format, or figures and reviews that are not the contract's: an error with
    the job's usage and trace kept, never a card with something missing (P14)."""
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(WROTE_READ | {"trace": trace("wrote")} | sent)
    assert await work(db, with_text(P), make(fake)) == {"card_service_failed": 1}
    stored = await cards.card(db, P.id)
    assert stored["card"] is None and said in stored["error"]
    assert await figures(db) == {} and await outbox_of(db, events.CARD_READY) == []
    (attempt,) = await attempts(db)
    assert attempt["failure"] == "service" and said in attempt["error"]
    assert attempt["usage"] == USAGE and attempt["trace"] == trace("wrote")
    # The service's failure counts against nothing: the pass is back in the queue, which pauses.
    (row,) = await deep_rows(db)
    assert (row["state"], row["failed_runs"]) == ("ready", 0)
    assert await cards.paused_until(db, deep_passes.PAUSE) is not None


async def test_a_failed_read_keeps_the_figures_of_the_read_before_it(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(WROTE_READ)
    d = make(fake)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    fake.outcome = {"error": "no valid read", "failure": "paper", "stats": USAGE}
    fake.outcome["reviews"] = REVIEWS[:1]
    await deep_passes.request(db, P.id)
    assert await work(db, with_text(P), d) == {"card_failed": 1}
    assert (await cards.card(db, P.id))["card"] == READ and len(await figures(db)) == len(FIGURES)
    assert (await attempts(db))[-1]["reviews"] == REVIEWS[:1]  # what ran is kept with the failure


async def test_a_text_of_unknown_version_is_submitted_as_such(db):
    """A paper of no known arXiv version is read from the archive's historical LaTeX, of no
    known version either: the submission says so, and the service takes the latest figures."""
    old = paper("2610.00003", title="A paper of no known version", version=None)
    await papers_store.upsert(db, [old])
    await deep_passes.request(db, old.id)
    held = FakeArchive()
    held.texts[old.id] = (None, BODY)
    fake = FakeDeep(DONE)
    assert await work(db, held, make(fake)) == {"cards_written": 1}
    (sent,) = fake.requests
    assert "body_version" in sent["paper"] and sent["paper"]["body_version"] is None


async def test_the_figure_endpoint_serves_a_reads_figures_to_the_web_app_alone(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(WROTE_READ)
    d = make(fake)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    dsn = f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"
    app = create_app(dsn, TOKEN, Config(), integrations=[d], read_token="r3ad")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        base = f"/api/papers/{P.id}/deep/figures"
        assert (await http.get(f"{base}/methodology_by_step")).status_code == 401
        read_token = {"Authorization": "Bearer r3ad"}
        assert (
            await http.get(f"{base}/methodology_by_step", headers=read_token)
        ).status_code == 403
        # As the paper page's <img> asks: the session cookie, and when the read was written.
        http.cookies.set("litwatch_token", TOKEN)
        written = (await http.get(f"/api/papers/{P.id}")).json()["card"]["written_at"]
        shown = await http.get(f"{base}/methodology_by_step", params={"v": written})
        assert shown.status_code == 200 and shown.content == BLUE
        assert shown.headers["content-type"] == "image/webp"
        assert shown.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert shown.headers["x-content-type-options"] == "nosniff"
        # Without the time, an empty one included, nothing says which read's figure it is.
        for unversioned in ({}, {"v": ""}):
            other = await http.get(f"{base}/channel_util_s1", params=unversioned)
            assert other.content == RUST
            assert other.headers["cache-control"] == "private, no-cache"
        # A name the read does not have, a figure of another paper's, and the page still its own.
        assert (await http.get(f"{base}/no_such_figure", params={"v": written})).status_code == 404
        assert (
            await http.get(f"/api/papers/{Q.id}/deep/figures/methodology_by_step")
        ).status_code == 404
        assert (await http.get(f"/api/papers/{P.id}")).json()["card"]["card"]["format"] == "read"
        # Written again: a tab still on the old read gets none of the new read's figures under
        # the old address, which it would keep for good; the new address has them. A figure
        # named "pdf" is the read's, never the paper's PDF.
        again = [sent_figure(f["name"], RUST) for f in FIGURES] + [sent_figure("pdf")]
        fake.outcome = WROTE_READ | {"figures": again}
        await deep_passes.request(db, P.id)
        assert await work(db, with_text(P), d) == {"cards_written": 1}
        stale = await http.get(f"{base}/methodology_by_step", params={"v": written})
        assert stale.status_code == 404
        now = (await http.get(f"/api/papers/{P.id}")).json()["card"]["written_at"]
        assert now != written
        fresh = await http.get(f"{base}/methodology_by_step", params={"v": now})
        assert fresh.status_code == 200 and fresh.content == RUST
        assert (
            await http.get(f"{base}/methodology_by_step", params={"v": "soon"})
        ).status_code == 404
        named_pdf = await http.get(f"{base}/pdf", params={"v": now})
        assert named_pdf.status_code == 200 and named_pdf.content == BLUE
        assert named_pdf.headers["content-type"] == "image/webp"
        assert (await http.get(f"{base}/pdf", params={"v": written})).status_code == 404
        http.cookies.clear()
        assert (await http.get(f"{base}/pdf", headers=read_token)).status_code == 403


async def test_the_read_token_fetches_a_papers_pdf(db):
    """The deep service cuts a PDF-only paper's figures from its pages (plans/deep-read.md, D2)."""
    dsn = f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"
    fake = FakeArchive(records={P.id: 2})
    fake.pdfs[P.id] = b"%PDF-1.7 the paper"
    app = create_app(dsn, TOKEN, Config(), read_token="r3ad", archive=archive(fake))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        got = await http.get(f"/api/papers/{P.id}/pdf", headers={"Authorization": "Bearer r3ad"})
        assert got.status_code == 200 and got.content == b"%PDF-1.7 the paper"
        assert got.headers["content-type"] == "application/pdf"
        # The version a read's text is of, asked for, is the version fetched, not the latest.
        pinned = await http.get(
            f"/api/papers/{P.id}/pdf",
            params={"version": 1},
            headers={"Authorization": "Bearer r3ad"},
        )
        assert pinned.status_code == 200
        assert [j["version"] for j in fake.jobs] == [2, 1]
        assert fake.pdf_versions == ["2", "1"]
        # Reading still, and nothing else: the token asks for no card.
        asked = await http.post(
            f"/api/papers/{P.id}/card", headers={"Authorization": "Bearer r3ad"}
        )
        assert asked.status_code == 403


REVIEW_NOTE = {
    "kind": "review",
    "source": "service",
    "note": "Still failing after two revisions: clarity 3; the check on an objective voice.",
}


async def test_deep_trace_prints_the_review_rounds_and_needs_you_shows_the_review_note(
    db, tmp_path, monkeypatch
):
    from typer.testing import CliRunner  # noqa: PLC0415

    from litwatch import cli  # noqa: PLC0415

    await grade(db, Fit.CENTRAL)
    failed = READ | {"process_notes": [*READ["process_notes"], REVIEW_NOTE]}
    fake = FakeDeep(WROTE_READ | {"result": failed, "reviews": REVIEWS[:1], "trace": RICH})
    d = make(fake)
    assert await work(db, with_text(P), d) == {"cards_written": 1}

    monkeypatch.chdir(tmp_path)  # every command loads ./.env
    monkeypatch.setenv(
        "LITWATCH_DATABASE_URL", f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"
    )
    config = tmp_path / "litwatch.toml"
    config.write_text('[schedule]\ntimezone = "UTC"\n')

    async def run(*args: str) -> str:
        argv = ["deep", *args, "--config", str(config)]
        result = await asyncio.to_thread(CliRunner().invoke, cli.app, argv)
        assert result.exit_code == 0, result.output
        return result.output

    shown = await run("trace", P.id)
    assert "Reviews: 1 round" in shown
    assert (
        "round 1 · failed · faithfulness 4, clarity 3, depth 5, calibration 4, followability 3"
        in shown
    )
    assert "retold: A memory module helps; what it is tested on is unclear." in shown
    assert "checks failed: voice" in shown
    assert "- [clarity] who/1: The sentence hides who ran the ablation." in shown
    assert "rewrite: The authors ablate the memory module." in shown
    assert "- [voice] glance: Says 'for you'." in shown
    assert f"review (service): {REVIEW_NOTE['note']}" in shown
    whole = json.loads(await run("trace", P.id, "--json"))
    assert whole["reviews"] == REVIEWS[:1] and whole["process_notes"][-1] == REVIEW_NOTE
    assert f"review (service): {REVIEW_NOTE['note']}" in await run("notes")

    # Needs you lists the read under Deep notes, its review note among the others, until the
    # owner acknowledges it.
    _, http = client(db, [d])
    async with http:
        (noted,) = (await http.get("/api/settings/quality")).json()["night"]["deep_notes"]
        assert noted["paper_id"] == P.id and noted["acknowledged_at"] is None
        assert [n["kind"] for n in noted["notes"]] == ["access", "review"]
        assert noted["notes"][-1] == REVIEW_NOTE
        seen = {"cards": [{"paper_id": P.id, "written_at": noted["written_at"]}]}
        marked = await http.post("/api/settings/acknowledge", json=seen)
        assert [c["paper_id"] for c in marked.json()["cards"]] == [P.id]


async def test_an_attempt_no_review_ran_for_prints_none(db, tmp_path, monkeypatch):
    from typer.testing import CliRunner  # noqa: PLC0415

    from litwatch import cli  # noqa: PLC0415

    await grade(db, Fit.CENTRAL)
    assert await work(db, with_text(P), make(FakeDeep(DONE | {"trace": RICH}))) == {
        "cards_written": 1
    }
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "LITWATCH_DATABASE_URL", f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"
    )
    config = tmp_path / "litwatch.toml"
    config.write_text('[schedule]\ntimezone = "UTC"\n')
    result = await asyncio.to_thread(
        CliRunner().invoke, cli.app, ["deep", "trace", P.id, "--config", str(config)]
    )
    assert result.exit_code == 0 and "Reviews" not in result.output


async def test_a_service_failure_counts_against_nothing_and_pauses_the_whole_queue(db):
    await grade(db, Fit.CENTRAL)
    await grade(db, Fit.CENTRAL, Q.id)
    signed_out = {"error": "not running on the ChatGPT subscription", "failure": "service"}
    fake = FakeDeep(signed_out | {"stats": USAGE, "trace": []})
    d = make(fake)
    assert await work(db, with_text(P, Q), d) == {"card_service_failed": 1}
    (failed,) = await attempts(db)
    assert (failed["failure"], failed["usage"]) == ("service", USAGE)
    assert failed["service_failed_at"] is not None
    (tried,) = await deep_rows(db, failed["paper_id"])
    assert (tried["state"], tried["failed_runs"]) == ("ready", 0)  # back in the queue, uncounted
    assert await work(db, with_text(P, Q), d) == {}  # paused: the next paper waits too
    assert len(fake.requests) == 1

    fake.outcome = DONE
    await unpause(db)
    assert await work(db, with_text(P, Q), d) == {"cards_written": 2}
    tried_first = failed["paper_id"]
    assert fake.requests[1]["paper"]["id"] != tried_first  # the failed one went to the back


async def deep_status(conn, d: Deep) -> dict:
    found = await monitor.facts(conn, Config(), [d], None)
    return next(c for c in monitor.components(found, Config()) if c["id"] == "deep")


async def test_the_deep_component_reads_the_sign_in_and_a_service_failure_until_a_card(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep({"error": "usage limit reached", "failure": "service", "trace": []})
    d = make(fake)
    assert (await deep_status(db, d))["status"] == "healthy"
    fake.auth = {"state": "missing", "detail": "no credential stored; run `login`"}
    signed_out = await deep_status(db, d)
    assert signed_out["status"] == "degraded"
    said = "sign-in missing (no credential stored; run `login`); expected ok"
    assert said in signed_out["reason"]

    fake.auth = {"state": "ok"}
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    failed = await deep_status(db, d)
    assert failed["status"] == "degraded"
    assert "service failure at" in failed["reason"] and "usage limit reached" in failed["reason"]
    # It holds after the pause, until the service makes a card again.
    await unpause(db)
    assert (await deep_status(db, d))["status"] == "degraded"
    fake.outcome = DONE
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    assert (await deep_status(db, d))["status"] == "healthy"


async def test_a_service_failure_the_job_outlived_is_not_told_with_the_papers_error(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep({"error": "no valid card", "failure": "paper", "stats": USAGE, "trace": []})
    fake.fail_polls = 1  # following the job fails once; the job goes on
    d = make(fake)
    await work(db, with_text(P), d)
    await unpause(db)
    await work(db, with_text(P), d)  # followed again: it ends as the paper's failure
    (ended,) = await attempts(db)
    assert (ended["failure"], ended["error"]) == ("paper", "no valid card")
    status = await deep_status(db, d)
    assert status["status"] == "degraded"  # until the next card
    assert "service failure at" in status["reason"] and "no valid card" not in status["reason"]


async def test_a_submission_whose_answer_was_lost_is_found_by_its_key_after_the_pause(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.lose_answers = 1  # the service took the job; its answer never came
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    (attempt,) = await attempts(db)
    assert attempt["ended_at"] is None and attempt["service_failed_at"] is not None
    assert "submitting failed" in attempt["error"]
    (row,) = await deep_rows(db)
    assert (row["state"], row["failed_runs"]) == ("running", 0)
    assert await work(db, with_text(P), d) == {}  # paused

    await unpause(db)
    # Found by its key and followed, without the archive (which has nothing now) or a new job.
    assert await work(db, FakeArchive(status="pending"), d) == {"cards_written": 1}
    assert fake.keys_sent == [f"{attempt['id']}:{P.id}"]
    (followed,) = await attempts(db)
    assert fake.started == 1 and followed["at"] == attempt["at"]  # still when it was submitted


async def test_a_lost_submission_the_service_never_ran_ends_and_the_pass_is_tried_afresh(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.lose_answers = 1
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    (first,) = await deep_rows(db)
    fake.keys.clear()  # the service restarted meanwhile
    await unpause(db)
    replaced = FakeArchive()
    replaced.texts[P.id] = (P.version, BODY + " Replaced.")  # the archive's text changed
    assert await work(db, replaced, d) == {"cards_written": 1}
    lost, made = await attempts(db)
    assert (lost["ended_at"] is not None, lost["usage"], lost["failure"]) == (True, None, None)
    assert fake.keys_sent == [f"{lost['id']}:{P.id}", f"{made['id']}:{P.id}"]
    assert made["usage"] == USAGE and fake.started == 2
    assert fake.requests[-1]["paper"]["body"].endswith("Replaced.")
    (row,) = await deep_rows(db)
    assert row["text_hash"] != first["text_hash"]  # the card's label is what was sent


async def test_a_lost_submission_with_no_job_waits_for_text_and_unsave_as_a_first_try(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.fail_submits = 1  # never reached the service
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    await unpause(db)
    assert await work(db, FakeArchive(status="pending"), d) == {"awaiting_text": 1}
    (attempt,) = await attempts(db)
    assert attempt["ended_at"] is not None and attempt["usage"] is None
    (row,) = await deep_rows(db)
    assert row["state"] == "awaiting_text"  # listed on Quality after a week, as any pass
    await word.toggle(db, P.id, Hand.UNSAVE)  # and an Unsave drops it, as any pass
    assert await deep_rows(db) == [] and fake.started == 0


async def test_a_submission_the_service_refused_is_tried_again_after_the_pause(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.fail_submits = 1
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    (row,) = await deep_rows(db)
    assert (row["state"], row["failed_runs"]) == ("running", 0)  # its attempt kept, uncounted
    assert await work(db, with_text(P), d) == {}  # paused
    await unpause(db)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    refused, made = await attempts(db)
    assert refused["service_failed_at"] is not None and refused["usage"] is None
    assert made["usage"] == USAGE and fake.started == 1


@pytest.mark.parametrize(
    ("status", "said"),
    [
        (413, "body too large"),
        (422, "paper.id, paper.title, and paper.body are required"),
        (400, 'unknown extension "faithfulness"; known: methods'),  # the configuration's
    ],
)
async def test_a_submission_the_service_turns_down_for_what_it_is_counts_and_pauses_nothing(
    db, status, said
):
    await grade(db, Fit.CENTRAL)
    await grade(db, Fit.CENTRAL, Q.id)
    fake = FakeDeep(DONE)
    fake.answer_submits = (status, said)
    # Not paused: the next paper is tried in the same run, and turned down as well.
    assert await work(db, with_text(P, Q), make(fake)) == {"card_failed": 2}
    assert await cards.paused_until(db, deep_passes.PAUSE) is None
    first, _ = await attempts(db)
    assert (first["failure"], first["service_failed_at"]) == ("paper", None)
    assert first["ended_at"] is not None and f"{status} {said}" in first["error"]
    # Nothing ran: its usage is known, and zero, not unknown.
    assert first["usage"] == deep_passes.NOTHING_RAN
    month = await cards.usage_since(db, first["at"])
    assert (month["attempts"], month["unknown"], month["list_cost"]) == (2, 0, 0.0)
    (row,) = await deep_rows(db)
    assert (row["state"], row["failed_runs"]) == ("failed", 1)
    assert fake.started == 0


@pytest.mark.parametrize("status", [503, 500, 429, 408, 404])
async def test_a_submission_failing_on_the_service_pauses_the_queue_and_counts_nothing(db, status):
    await grade(db, Fit.CENTRAL)
    await grade(db, Fit.CENTRAL, Q.id)
    fake = FakeDeep(DONE)
    fake.answer_submits = (status, "not now")
    assert await work(db, with_text(P, Q), make(fake)) == {"card_service_failed": 1}
    assert await cards.paused_until(db, deep_passes.PAUSE) is not None
    (attempt,) = await attempts(db)
    assert attempt["failure"] is None and attempt["service_failed_at"] is not None
    assert attempt["ended_at"] is None  # its key is looked up after the pause
    (row,) = await deep_rows(db, attempt["paper_id"])
    assert (row["state"], row["failed_runs"]) == ("running", 0)


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
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    (row,) = await deep_rows(db)
    assert row["state"] == "running" and "following the deep job" in row["last_error"]
    (attempt,) = await attempts(db)
    assert attempt["ended_at"] is None and attempt["service_failed_at"] is not None
    assert await work(db, with_text(P), d) == {}  # the queue waits
    await unpause(db)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    assert len(fake.requests) == 1


async def test_a_submitted_job_is_followed_past_a_dismissal(db):
    await deep_passes.request(db, Q.id)
    fake = FakeDeep(DONE)
    fake.fail_polls = 1
    d = make(fake)
    assert await work(db, with_text(Q), d) == {"card_service_failed": 1}
    await db.execute("UPDATE papers SET dismissed_at = now() WHERE id = %s", (Q.id,))
    await unpause(db)
    assert await work(db, with_text(Q), d) == {"cards_written": 1}
    assert [(a["usage"], a["ended_at"] is not None) for a in await attempts(db)] == [(USAGE, True)]
    assert len(fake.requests) == 1


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
    d = make(FakeDeep(DONE))
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


async def test_a_job_unreachable_past_its_deadline_is_kept_until_the_service_answers(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.fail_polls = 1
    d = make(fake)
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    await db.execute("UPDATE deep_attempts SET at = now() - interval '2 hours'")
    await unpause(db)
    fake.fail_polls = 1  # unreachable past the deadline: it may yet run, so it is kept
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    (attempt,) = await attempts(db)
    assert attempt["ended_at"] is None
    (row,) = await deep_rows(db)
    assert (row["state"], row["failed_runs"]) == ("running", 0)
    await unpause(db)
    fake.vanish = True  # back, restarted: the job is gone, so the attempt ends
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    (attempt,) = await attempts(db)
    assert (attempt["usage"], attempt["failure"]) == (None, "service")
    assert "vanished" in attempt["error"] and fake.started == 1


def hurried(fake: FakeDeep) -> Deep:
    """A client that gives up on a job a moment after submitting it."""
    client = DeepClient("http://deep.test", http=mock(fake.handle), poll_seconds=0)
    client.timeout_seconds = 0.2
    return Deep(client)


async def test_a_job_given_up_on_at_its_deadline_is_kept_and_its_card_taken_once_it_ends(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.hold = True
    d = hurried(fake)
    assert await work(db, with_text(P), d) == {"card_service_failed": 1}
    (attempt,) = await attempts(db)
    assert attempt["ended_at"] is None and attempt["service_failed_at"] is not None
    (row,) = await deep_rows(db)
    assert (row["state"], row["failed_runs"]) == ("running", 0)
    assert await work(db, with_text(P), d) == {}  # paused

    fake.hold = False  # it ends during the pause
    await unpause(db)
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    (made,) = await attempts(db)
    assert made["usage"] == USAGE and fake.started == 1


async def test_a_job_running_past_its_deadline_by_the_services_clock_is_given_up_on(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.hold = True
    fake.started_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()  # stuck
    assert await work(db, with_text(P), hurried(fake)) == {"card_service_failed": 1}
    (ended,) = await attempts(db)
    assert ended["ended_at"] is not None and "running past its deadline" in ended["error"]
    assert (ended["failure"], ended["usage"]) == ("service", None)
    (row,) = await deep_rows(db)
    assert (row["state"], row["failed_runs"]) == ("ready", 0)  # uncounted, tried afresh


async def test_a_job_queued_past_its_deadline_is_kept_however_often_it_is_followed(db):
    await grade(db, Fit.CENTRAL)
    fake = FakeDeep(DONE)
    fake.queued = True  # behind another job: its own clock has not started
    d = hurried(fake)
    for _ in range(3):
        assert await work(db, with_text(P), d) == {"card_service_failed": 1}
        (attempt,) = await attempts(db)
        assert attempt["ended_at"] is None
        await unpause(db)
    fake.queued = False
    assert await work(db, with_text(P), d) == {"cards_written": 1}
    assert fake.started == 1 and len(await attempts(db)) == 1


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


async def test_every_due_automatic_pass_is_worked_however_many_cards_were_made_today(db):
    for _ in range(10):  # what the old daily cap allowed in 24 hours
        await cards.add_attempt(db, Q.id, None, automatic=True)
    await grade(db, Fit.CENTRAL)
    await grade(db, Fit.CENTRAL, Q.id)
    d = make(FakeDeep(DONE))
    assert await work(db, with_text(P, Q), d) == {"cards_written": 2}


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

    # Automatic, and the paper page shows why it is queued.
    _, http = client(db, [make(FakeDeep(DONE))])
    async with http:
        page = (await http.get(f"/api/papers/{P.id}")).json()
    assert page["card_pass"]["reason"] == "pick"
    fake = FakeDeep(DONE)
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


def client(conn, integrations, config: Config | None = None) -> tuple:
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, config or Config(), integrations=integrations)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    return app, http


async def test_the_owner_may_ask_for_a_card_an_automatic_pass_waits_for(db):
    """While the automatic pass waits its turn, asking takes it over."""
    await grade(db, Fit.CENTRAL)
    _, http = client(db, [make(FakeDeep(DONE))])
    async with http:
        page = (await http.get(f"/api/papers/{P.id}")).json()
        asked = await http.post(f"/api/papers/{P.id}/card")
        after = (await http.get(f"/api/papers/{P.id}")).json()
    assert (page["card_pass"]["reason"], page["card_requestable"]) == ("focus", True)
    assert asked.status_code == 202 and asked.json()["pass_id"] == page["card_pass"]["id"]
    assert (after["card_pass"]["reason"], after["card_requestable"]) == ("request", False)


async def test_deep_read_on_a_paper_litwatch_lacks_fetches_it_first(db, monkeypatch):
    """Deep read outside the collection: an archive paper not fetched yet is fetched as
    Process fetches it, then its card is asked for and its Process job queued; arXiv lacking it
    is a 404."""
    fetched = []
    found = {"2610.09999": paper("2610.09999", title="Elsewhere")}
    monkeypatch.setattr(
        misses,
        "fetch_by_ids",
        answering(lambda ids: fetched.append(ids) or [found[i] for i in ids if i in found]),
    )
    scheduled = Config(schedule=Schedule(enabled=True))  # the jobs worker runs
    _, http = client(db, [make(FakeDeep(DONE))], scheduled)
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
    assert page["process"]["state"] == "queued"  # outside the collection, processed now too
    assert await papers_store.get(db, "2610.08888") is None


async def test_deep_read_asked_again_while_its_process_job_runs_changes_nothing(db):
    """A retry of Deep read while the Process job it started runs: the same pass answers, and
    the job is not asked again, so it ends done, not queued."""
    scheduled = Config(schedule=Schedule(enabled=True))  # the jobs worker runs
    _, http = client(db, [make(FakeDeep(DONE))], scheduled)
    async with http:
        asked = await http.post(f"/api/papers/{P.id}/card")
        job = (await jobs_store.claim(db))["id"]
        again = await http.post(f"/api/papers/{P.id}/card")
    assert again.status_code == 202 and again.json() == asked.json()
    await jobs_store.finish(db, job, "done")
    assert (await jobs_store.get(db, job))["state"] == "done"


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
