"""The full-text queue: which papers get a pass and when (the owner's word included), working a
pass against the archive and the `brief` role, what it writes and settles, its failures, the
lease, revisions, and what the owner sees of it."""

import asyncio
import json
from collections import Counter
from datetime import timedelta

import httpx
import pytest

from litwatch import fulltext
from litwatch.archive import URGENT, PaperText, PdfText
from litwatch.config import Config, Models, RoleConfig
from litwatch.core import (
    FAILED_RUNS,
    Cause,
    Fit,
    Label,
    LabelOrigin,
    LabelValue,
    Status,
    Verdict,
)
from litwatch.core.fulltext import (
    LEASE,
    RECONCILE_LOOKBACK,
    TEXT_PRIORITY,
    TEXT_WAVE,
    PassKind,
    PassState,
    TextPass,
    to_queue,
    to_relabel,
    wanted,
)
from litwatch.feedback import word
from litwatch.fulltext import brief, trim
from litwatch.fulltext import worker as text_worker
from litwatch.fulltext.worker import TextPassWorker
from litwatch.gates.database import LOCKS
from litwatch.gates.work import WorkGate
from litwatch.jobs import nightly
from litwatch.llm import Gateway, ProviderError
from litwatch.store import analyses as analyses_store
from litwatch.store import digests as digests_store
from litwatch.store import fulltext as fulltext_store
from litwatch.store import papers as papers_store
from litwatch.store import runs, verdicts
from litwatch.store import taxonomy as taxonomy_store
from tests import sessions, settling
from tests.briefs import (
    ANSWER,
    BODY,
    BRIEF,
    BRIEF_ROLE,
    CONFIG,
    GIST,
    P,
    Writer,
    analysed,
    archive,
    client,
    graded,
    passes,
    ready,
    work,
)
from tests.conftest import FAITH, INTERP, TREE, gate_on, paper
from tests.fakes import ScriptedProvider
from tests.fakes_http import FakeArchive
from tests.scripted import refusal
from tests.sessions import Bridge, Session, in_read, in_write
from tests.working import WAIT, items_of, serving, until


@pytest.fixture
async def db(conn: Session):
    """P classified, its primary class Interpretability (an Area, over the CoT faithfulness
    Focus topic)."""
    await settling.save_tree(conn, TREE)
    await in_write(conn, papers_store.upsert, [P])
    await conn.execute(
        "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
        "primary_class, model) VALUES (%s, 1, 'x', 'interpretability', 'm')",
        (P.id,),
    )
    return conn


async def events(conn: Session, *types: str) -> list[dict]:
    rows = await (
        await conn.execute(
            "SELECT type, payload FROM outbox WHERE type = ANY(%s) ORDER BY id", (list(types),)
        )
    ).fetchall()
    return [{"type": r["type"], **r["payload"]} for r in rows]


async def retry_failed(conn: Session) -> int:
    """`store.fulltext.retry_failed` in a unit of its own, as the nightly calls it."""
    async with Bridge(conn).write("retry failed", taxonomy="shared") as unit:
        return await fulltext_store.retry_failed(unit, FAILED_RUNS)


async def record(conn: Session, attempt: fulltext.Attempt) -> bool:
    """Record a pass's attempt as the worker does, in a change of its paper."""
    return await fulltext.record_in_change(Bridge(conn), attempt, settling.RULES)


async def current_gist(conn: Session) -> str | None:
    found = await conn.execute(
        f"SELECT {analyses_store.CURRENT_GIST} AS gist FROM papers p WHERE p.id = %s", (P.id,)
    )
    return (await found.fetchone())["gist"]


# -- which papers get a pass -----------------------------------------------------------------------


def test_the_pass_a_paper_wants_and_when_it_is_queued_again():
    assert wanted(focus=True, area=True, has_text_result=False) == (PassKind.BRIEF, "focus")
    assert wanted(focus=False, area=True, has_text_result=False) == (PassKind.WEIGHT, "area")
    assert wanted(focus=False, area=False, has_text_result=True) == (PassKind.BRIEF, "revised")
    assert wanted(focus=False, area=False, has_text_result=False) is None
    want = (PassKind.BRIEF, "focus")
    done = TextPass(1, PassKind.BRIEF, "focus", PassState.DONE, 2)
    assert to_queue(want, None, 2)
    assert not to_queue(want, done, 2)
    assert to_queue(want, done, 3)  # a new arXiv version
    assert not to_queue(want, done, None)  # no version known, and no update since
    newer = TextPass(1, PassKind.BRIEF, "focus", PassState.DONE, 2, newer_update=True)
    assert to_queue(want, newer, None)  # an update of unknown version since
    assert not to_queue(want, TextPass(1, PassKind.BRIEF, "focus", PassState.FAILED, None), 3)
    area = TextPass(1, PassKind.WEIGHT, "area", PassState.AWAITING_TEXT, None)
    assert to_relabel(want, area)
    assert not to_relabel(want, done)  # done: its result stands
    assert not to_relabel((PassKind.WEIGHT, "area"), area)


async def test_a_focus_paper_at_central_or_related_gets_a_brief_and_adjacent_none(db: Session):
    await graded(db, "adjacent")
    assert await passes(db) == []
    await graded(db, "related")
    (row,) = await passes(db)
    assert (row["kind"], row["reason"], row["state"], row["paper_revision"]) == (
        "brief",
        "focus",
        "awaiting_text",
        1,
    )
    await graded(db, "central")
    assert len(await passes(db)) == 1  # still the one pass


async def test_an_area_must_read_gets_a_weight_re_check(db: Session):
    await analysed(db, "incremental")
    await graded(db, "central", INTERP.slug)  # central and incremental: relevant
    assert await passes(db) == []
    await analysed(db, "solid")  # central and solid: must read
    (row,) = await passes(db)
    assert (row["kind"], row["reason"]) == ("weight", "area")
    await graded(db, "related")  # now a Focus paper too: the pass not started says so
    (row,) = await passes(db)
    assert (row["kind"], row["reason"]) == ("brief", "focus")


async def test_the_owners_word_queues_a_pass_and_takes_back_one_not_started(db: Session):
    await graded(db, "adjacent")
    said = Label(
        paper_id=P.id, topic=FAITH.slug, value=LabelValue.CENTRAL, origin=LabelOrigin.READER
    )
    await settling.label(db, said)
    assert [r["kind"] for r in await passes(db)] == ["brief"]
    await settling.label(db, said.model_copy(update={"value": LabelValue.ADJACENT}))
    assert await passes(db) == []  # not started and no longer wanted: dropped


async def test_a_paused_topic_queues_nothing_and_drops_its_pass(db: Session):
    await graded(db, "related")  # relevant: the Area above would not pick it
    assert len(await passes(db)) == 1
    tree = await in_read(db, taxonomy_store.load)
    faith = next(t for t in tree.topics if t.slug == FAITH.slug)
    await settling.save_taxonomy(
        db, tree.following(faith.model_copy(update={"status": Status.PAUSED}))
    )
    assert await passes(db) == []


async def test_an_area_counts_a_must_read_its_descendant_graded(db: Session):
    async def status(slug: str, value: Status) -> None:
        tree = await in_read(db, taxonomy_store.load)
        topic = next(t for t in tree.topics if t.slug == slug)
        await settling.save_taxonomy(db, tree.following(topic.model_copy(update={"status": value})))

    await analysed(db, "solid")
    await graded(db, "central")  # must read in the Focus topic, ungraded by the Area itself
    await status(FAITH.slug, Status.PAUSED)
    # Still the Area's pick candidate through its followed descendant (pipeline.md, section 4).
    assert [(r["kind"], r["reason"]) for r in await passes(db)] == [("weight", "area")]
    await status(INTERP.slug, Status.PAUSED)
    assert await passes(db) == []
    await status(INTERP.slug, Status.ACTIVE)  # the Area's own papers include its subtree's
    assert [(r["kind"], r["reason"]) for r in await passes(db)] == [("weight", "area")]
    # Moved out from under the Area, the class's papers are no longer its pick candidates.
    tree = await in_read(db, taxonomy_store.load)
    moved = [
        c.model_copy(update={"parent": None}) if c.slug == FAITH.slug else c for c in tree.classes
    ]
    await settling.save_taxonomy(db, tree.model_copy(update={"classes": moved}))
    assert await passes(db) == []


@pytest.mark.parametrize(
    "attempt",
    [{"waiting": True}, {"error": "no text", "counts": True}],
    ids=["still-fetching", "failed"],
)
async def test_a_pass_running_when_its_topic_is_paused_is_dropped_once_it_stops(
    db: Session, attempt
):
    await graded(db, "related")
    (claimed,) = await in_write(db, fulltext_store.claim, LEASE)
    tree = await in_read(db, taxonomy_store.load)
    faith = next(t for t in tree.topics if t.slug == FAITH.slug)
    await settling.save_taxonomy(
        db, tree.following(faith.model_copy(update={"status": Status.PAUSED}))
    )
    assert len(await passes(db)) == 1  # running: left to finish
    assert await record(db, fulltext.Attempt(claimed, **attempt))
    assert await passes(db) == []  # back in the queue, no longer wanted: dropped


# -- working a pass --------------------------------------------------------------------------------


async def test_a_pass_awaits_its_text_then_writes_brief_gist_and_weight(db: Session):
    await graded(db, "related")  # related and solid (no Weight yet): relevant
    fake, writer = FakeArchive(status="pending"), Writer()
    assert await work(db, fake, writer) == {"awaiting_text": 1}
    assert [(j["kind"], j["version"], j["priority"]) for j in fake.jobs] == [("source", 1, URGENT)]
    (row,) = await passes(db)
    assert (row["state"], row["lease_until"]) == ("awaiting_text", None)
    assert writer.asked == []

    fake.status = "complete"
    assert await work(db, ready(fake), writer) == {"briefs_written": 1}
    (asked,) = writer.asked
    assert "Area: Interpretability" in asked and BODY.strip() in asked
    (row,) = await passes(db)
    assert (row["state"], row["arxiv_version"], row["text_source"]) == ("done", 1, "latex")
    assert len(row["text_hash"]) == 64

    (b,) = await (await db.execute("SELECT * FROM briefs")).fetchall()
    assert (b["text"], b["overstatement"], b["paper_revision"], b["pass_id"]) == (
        BRIEF,
        None,
        1,
        row["id"],
    )
    # The full text's Gist and Weight are the current ones, and the grade followed the Weight.
    assert (await in_read(db, analyses_store.weight_of, P.id))["source"] == "full_text"
    assert await current_gist(db) == GIST
    (grade,) = await (await db.execute("SELECT grade FROM effective_grades")).fetchall()
    assert grade["grade"] == "must_read"  # related and landmark
    written = await events(db, "brief.written", "gist.written")
    assert [(e["type"], e["revision"]) for e in written] == [
        ("brief.written", 1),
        ("gist.written", 1),
    ]
    assert written[1]["source"] == "full_text"
    run = await (await db.execute("SELECT * FROM runs WHERE kind = 'fulltext'")).fetchone()
    assert run["counts"]["briefs_written"] == 1 and run["finished_at"] is not None


async def test_an_abstract_gist_written_later_does_not_replace_the_full_texts(db: Session):
    await graded(db, "central")
    await work(db, ready(FakeArchive()), Writer())
    await analysed(db, "incremental")  # an analysis that ran late: stored, not current
    assert (await in_read(db, analyses_store.weight_of, P.id))["weight"] == "landmark"
    assert await current_gist(db) == GIST
    assert [e["source"] for e in await events(db, "gist.written")] == ["full_text"]


async def test_failures_count_only_without_text_or_a_usable_answer(db: Session):
    await graded(db, "central")
    fake, writer = FakeArchive(status="failed"), Writer()
    assert await work(db, fake, writer) == {"fulltext_failed": 1}
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"]) == ("failed", 1)
    assert "source failed; PDF failed" in row["last_error"]
    assert await work(db, fake, writer) == {}  # failed: tried again on the next run only

    fake.status = "complete"
    ready(fake)
    writer.answers = [ProviderError("down", transient=True)] * 2  # with the gateway's retry
    await retry_failed(db)
    await work(db, fake, writer)  # the provider did not answer: not counted
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"], row["arxiv_version"]) == ("failed", 1, 1)
    writer.answers = ["{not json"] * 2
    await retry_failed(db)
    await work(db, fake, writer)  # an invalid answer counts
    assert (await passes(db))[0]["failed_runs"] == 2


async def test_a_pass_tried_again_has_the_archive_try_again_what_it_gave_up_on(db: Session):
    await graded(db, "central")
    fake = FakeArchive(status="failed")
    assert await work(db, fake, Writer()) == {"fulltext_failed": 1}
    assert fake.retried == []  # a first try takes the archive's word
    await retry_failed(db)
    await in_write(db, fulltext_store.claim, LEASE)  # taken by a worker that died before reading
    await db.execute(
        "UPDATE fulltext_passes SET lease_until = now() - interval '1 second' WHERE kind <> 'deep'"
    )
    assert await work(db, fake, Writer()) == {"awaiting_text": 1}
    assert fake.retried == [3, 4]  # the source and the PDF it falls back on, asked for again
    fake.statuses.clear()  # and failed again
    assert await work(db, fake, Writer()) == {"fulltext_failed": 1}
    assert fake.retried == [3, 4]  # once
    (row,) = await passes(db)
    assert (row["failed_runs"], row["retry_text"]) == (2, False)


async def test_a_retry_covers_the_pdf_when_the_source_has_no_latex(db: Session):
    await graded(db, "central")
    fake = FakeArchive()  # the source is downloaded, but has no LaTeX text
    fake.statuses["pdf"] = "failed"
    assert await work(db, fake, Writer()) == {"fulltext_failed": 1}
    await retry_failed(db)
    assert await work(db, fake, Writer()) == {"awaiting_text": 1}
    assert [fake.jobs[i - 1]["kind"] for i in fake.retried] == ["pdf"]


async def test_a_pass_stopped_after_three_runs_waits_for_the_owner_who_runs_it_now(db: Session):
    await graded(db, "central")
    fake, writer = FakeArchive(status="failed"), Writer()
    for _ in range(FAILED_RUNS):
        await retry_failed(db)
        await work(db, fake, writer)  # tried again: the archive is asked to try again first
        fake.statuses.clear()  # which fails again
        await work(db, fake, writer)
    assert await retry_failed(db) == 0  # no longer tried
    (row,) = await passes(db)

    await db.execute("INSERT INTO runs (kind, finished_at) VALUES ('nightly', now())")
    await db.execute("DELETE FROM work_items")
    _, http = client(db)
    async with http:
        status = (await http.get("/api/summary")).json()["status"]
        quality = (await http.get("/api/settings/quality")).json()
        page = (await http.get(f"/api/papers/{P.id}")).json()
        now = await http.post(f"/api/settings/fulltext/{row['id']}/now")
        again = await http.post(f"/api/settings/fulltext/{row['id']}/now")
        missing = await http.post("/api/settings/fulltext/999999/now")
    assert (status["state"], status["fulltext_failed"]) == ("attention", 1)
    (listed,) = quality["night"]["fulltext"]
    assert (listed["id"], listed["kind"], listed["failed_runs"]) == (row["id"], "brief", 3)
    assert [(s["kind"], s["pass_id"]) for s in page["states"]] == [("fulltext_failed", row["id"])]
    assert page["text_pass"]["state"] == "failed" and page["brief"] is None
    assert now.status_code == 204
    assert [i["state"] for i in await items_of(db, "fulltext", str(row["id"]))] == ["pending"]
    assert (again.status_code, missing.status_code) == (409, 404)  # no longer waiting; none
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"]) == ("ready", 0)
    await work(db, ready(FakeArchive()), writer)
    assert (await passes(db))[0]["state"] == "done"


async def test_a_pass_every_brief_model_refused_stops_at_once_and_runs_again_from_the_top(
    db: Session, gate
):
    # models.md, roles: a refusal goes down the list at once; the whole list refusing stops it.
    fallback = RoleConfig(provider="openrouter", model="test-brief-2")
    listed = Config(models=Models(brief=BRIEF_ROLE.model_copy(update={"fallbacks": (fallback,)})))
    asked: list[str] = []
    refusing = True

    def respond(role: RoleConfig, request):
        asked.append(role.model)
        return refusal(role.model) if refusing else json.dumps(ANSWER)

    def gateway(config: Config, journal) -> Gateway:
        providers = {n: ScriptedProvider(n, respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=(0.0,))

    await graded(db, "central")
    fake = ready(FakeArchive())
    worked = await fulltext.work(gate, listed, archive(fake), gateway=gateway)
    assert worked == {"fulltext_failed": 1}
    assert asked == [BRIEF_ROLE.model, fallback.model]
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"], row["refused"]) == ("failed", FAILED_RUNS, True)
    assert await retry_failed(db) == 0  # not tried again
    # Terminal at once: no digest waits for it (digests.md, which papers a digest covers).
    waits = await db.execute(
        f"SELECT {digests_store._RECHECKING} AS waits FROM papers p WHERE p.id = %(p)s",
        {"p": P.id} | fulltext_store.stuck_params(FAILED_RUNS),
    )
    assert (await waits.fetchone())["waits"] is False
    app, http = client(db)
    async with http:
        (listed_pass,) = (await http.get("/api/settings/quality")).json()["night"]["fulltext"]
        page = (await http.get(f"/api/papers/{P.id}")).json()
        assert (await http.post(f"/api/settings/fulltext/{row['id']}/now")).status_code == 204
    assert listed_pass["refused"] is True
    assert [(s["kind"], s["refused"]) for s in page["states"]] == [("fulltext_failed", True)]
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"], row["refused"]) == ("ready", 0, False)
    refusing, asked[:] = False, []
    worked = await fulltext.work(gate, listed, archive(fake), gateway=gateway)
    assert worked == {"briefs_written": 1}
    assert asked == [BRIEF_ROLE.model]  # the owner's run starts from the role's own model


async def test_only_an_unusable_answer_starts_the_next_try_further_down_the_list(db: Session, gate):
    # models.md, roles: a run that found no text counts, but asked no model.
    fallback = RoleConfig(provider="openrouter", model="test-brief-2")
    listed = Config(models=Models(brief=BRIEF_ROLE.model_copy(update={"fallbacks": (fallback,)})))
    asked: list[str] = []

    def respond(role: RoleConfig, request):
        asked.append(role.model)
        return "{not json" if role.model == BRIEF_ROLE.model else json.dumps(ANSWER)

    def gateway(config: Config, journal) -> Gateway:
        providers = {n: ScriptedProvider(n, respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=(0.0,))

    await graded(db, "central")
    fake = FakeArchive(status="failed")
    worked = await fulltext.work(gate, listed, archive(fake), gateway=gateway)
    assert worked == {"fulltext_failed": 1}
    fake.status = "complete"
    ready(fake)
    await retry_failed(db)
    worked = await fulltext.work(gate, listed, archive(fake), gateway=gateway)
    assert worked == {"fulltext_failed": 1}
    assert asked == [BRIEF_ROLE.model]  # after the run without text, the role's own model
    (row,) = await passes(db)
    assert (row["failed_runs"], row["unusable_runs"]) == (2, 1)
    await retry_failed(db)
    worked = await fulltext.work(gate, listed, archive(fake), gateway=gateway)
    assert worked == {"briefs_written": 1}
    assert asked == [BRIEF_ROLE.model, fallback.model]


async def test_use_the_pdf_runs_a_pass_waiting_for_the_owner_on_the_papers_pdf(
    db: Session, monkeypatch
):
    # DESIGN.md, Needs you: "Try again, Use the PDF" on a full-text failure.
    read = PdfText("From the PDF. " * 40, ((0, 560),))
    monkeypatch.setattr("litwatch.archive.text.read_pdf", lambda pdf: read)
    await graded(db, "central")
    fake, writer = FakeArchive(status="failed"), Writer()
    await work(db, fake, writer)
    await db.execute("UPDATE fulltext_passes SET failed_runs = %s", (FAILED_RUNS,))
    (row,) = await passes(db)
    await db.execute("DELETE FROM work_items")
    _, http = client(db)
    async with http:
        used = await http.post(f"/api/settings/fulltext/{row['id']}/pdf")
        again = await http.post(f"/api/settings/fulltext/{row['id']}/pdf")
        missing = await http.post("/api/settings/fulltext/999999/pdf")
    assert used.status_code == 204
    assert [i["state"] for i in await items_of(db, "fulltext", str(row["id"]))] == ["pending"]
    assert (again.status_code, missing.status_code) == (409, 404)
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"], row["use_pdf"]) == ("ready", 0, True)
    fake = ready(FakeArchive())  # the LaTeX reads now, but the owner asked for the PDF
    assert await work(db, fake, writer) == {"briefs_written": 1}
    assert {j["kind"] for j in fake.jobs} == {"pdf"}  # its source never asked for
    (row,) = await passes(db)
    assert (row["state"], row["text_source"]) == ("done", "pdf")
    brief = await in_read(db, fulltext_store.brief_for, P.id)
    assert brief["source"] == "pdf"


async def test_a_pass_awaiting_text_over_a_week_waits_for_the_owner(db: Session):
    await graded(db, "central")
    assert await in_read(db, fulltext_store.count_stuck, FAILED_RUNS) == 0
    await db.execute(
        "UPDATE fulltext_passes SET queued_at = now() - interval '8 days' WHERE kind <> 'deep'"
    )
    (listed,) = await in_read(db, fulltext_store.stuck, FAILED_RUNS)
    assert listed["state"] == "awaiting_text"
    assert await in_read(db, fulltext_store.count_stuck, FAILED_RUNS) == 1


async def test_the_nightly_run_retries_failed_passes_and_works_the_queue(
    db: Session, gate, monkeypatch
):
    await analysed(db, "solid")  # nothing left for the night's other steps
    await graded(db, "central")
    await db.execute(
        "UPDATE fulltext_passes SET state = 'failed', failed_runs = 1 WHERE kind <> 'deep'"
    )
    fake = ready(FakeArchive())
    monkeypatch.setattr(fulltext, "archive_for", lambda config: archive(fake))
    run_id = await nightly.run(gate, CONFIG, fetch=False, gateway=Writer().gateway)
    counts = (await in_read(db, runs.get, run_id))["counts"]
    assert (counts["fulltext_retried"], counts["briefs_written"]) == (1, 1)
    assert counts["calls"]["brief"]["calls"] == 1  # on the nightly run
    assert (await passes(db))[0]["state"] == "done"


async def test_failed_passes_are_retried_only_after_a_taxonomy_save_in_flight(db: Session):
    await graded(db, "central")
    await db.execute(
        "UPDATE fulltext_passes SET state = 'failed', failed_runs = 1 WHERE kind <> 'deep'"
    )
    async with await sessions.connect(db.test_dsn, db.test_schema) as other:
        async with other.transaction():
            # As a save that drops passes holds it.
            await other.execute("SELECT pg_advisory_xact_lock(%s)", (LOCKS["taxonomy"].key,))
            retry = asyncio.create_task(retry_failed(db))
            await asyncio.sleep(0.3)
            assert not retry.done()  # taxonomy first, as every settle: no deadlock
        assert await retry == 1


# -- the backstop: the database, not the triggers, decides -----------------------------------------


async def test_a_pass_its_paper_stopped_qualifying_for_is_dropped_at_claim_with_no_call(
    db: Session,
):
    await graded(db, "central")
    await db.execute("UPDATE verdicts SET fit = 'none'")  # by a route that settles nothing
    fake, writer = ready(FakeArchive()), Writer()
    assert await work(db, fake, writer) == {"fulltext_dropped": 1}
    assert (writer.asked, fake.jobs, await passes(db)) == ([], [], [])


async def test_a_pass_wanted_for_another_reason_at_claim_runs_on_its_next_claim(db: Session):
    await analysed(db, "solid")
    await graded(db, "central")
    await graded(db, "central", INTERP.slug)  # must read in the Area too
    await db.execute("UPDATE verdicts SET fit = 'adjacent' WHERE topic = %s", (FAITH.slug,))
    fake, writer = ready(FakeArchive()), Writer()
    assert await work(db, fake, writer) == {"fulltext_relabelled": 1}  # nothing spent
    assert (writer.asked, fake.jobs) == ([], [])
    (row,) = await passes(db)
    assert (row["kind"], row["reason"], row["state"]) == ("weight", "area", "awaiting_text")
    assert await work(db, fake, writer) == {"briefs_written": 1}


async def test_a_claimed_pass_settles_its_paper_once_in_each_unit(db: Session, monkeypatch):
    # The claim's judgement settles the paper itself, and its change leaves it settled.
    await graded(db, "central")
    settles: Counter[tuple[int, str]] = Counter()
    units: list[object] = []  # kept alive, so no two units share an id
    names: set[str] = set()
    settle_many = word.settle_many

    async def counted(unit, paper_ids, **kwargs):
        ids = sorted(set(paper_ids))
        units.append(unit)
        settles.update((id(unit), paper_id) for paper_id in ids)
        names.update(unit.name for _ in ids)
        return await settle_many(unit, ids, **kwargs)

    monkeypatch.setattr(word, "settle_many", counted)
    assert await work(db, ready(FakeArchive()), Writer()) == {"briefs_written": 1}
    assert "judge claimed pass" in names
    assert max(settles.values()) == 1


async def test_a_paper_dismissed_after_its_pass_is_claimed_spends_nothing(db: Session, monkeypatch):
    await graded(db, "central")
    claim = fulltext_store.claim

    async def claim_then_dismiss(unit, *args, **kwargs):
        claimed = await claim(unit, *args, **kwargs)
        await papers_store.dismiss(unit, P.id)
        return claimed

    monkeypatch.setattr(fulltext_store, "claim", claim_then_dismiss)
    fake, writer = ready(FakeArchive()), Writer()
    assert await work(db, fake, writer) == {"fulltext_dismissed": 1}
    assert (writer.asked, fake.jobs) == ([], [])
    (row,) = await passes(db)
    assert row["state"] == "awaiting_text"  # left for the owner's word, as `record` leaves it


async def test_a_pass_whose_paper_was_rewritten_after_its_claim_is_dropped_unread(
    db: Session, monkeypatch
):
    await graded(db, "central")
    judge = fulltext.still_wanted
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})

    async def rewritten_then_judged(*args):  # rewritten once claimed, before it is judged
        await settling.settle_all(
            db, (await in_write(db, papers_store.upsert, [rewrite])).rewritten, cause=Cause.VERDICT
        )
        return await judge(*args)

    monkeypatch.setattr(fulltext, "still_wanted", rewritten_then_judged)
    fake, writer = ready(FakeArchive()), Writer()
    assert await work(db, fake, writer, limit=1) == {"fulltext_dropped": 1}
    assert (writer.asked, fake.jobs) == ([], [])
    assert [(r["paper_revision"], r["state"]) for r in await passes(db)] == [(2, "awaiting_text")]


async def test_the_reconcile_leaves_out_a_paper_its_area_grades_below_must_read(db: Session):
    await analysed(db, "solid")
    await graded(db, "central")  # must read in the Focus topic
    await graded(db, "adjacent", INTERP.slug)  # the Area's own grade stands over it
    tree = await in_read(db, taxonomy_store.load)
    faith = next(t for t in tree.topics if t.slug == FAITH.slug)
    await settling.save_taxonomy(
        db, tree.following(faith.model_copy(update={"status": Status.PAUSED}))
    )
    assert await passes(db) == []
    await db.execute("UPDATE verdicts SET recorded_at = now() - interval '3 days'")
    assert await in_read(db, fulltext_store.candidates, RECONCILE_LOOKBACK) == []


async def test_a_paper_that_came_to_qualify_by_no_trigger_gets_its_pass_on_the_nightly_run(
    db: Session, gate, monkeypatch
):
    await analysed(db, "solid")  # nothing left for the night's other steps
    found = Verdict(
        paper_id=P.id, topic=FAITH.slug, topic_version="v", fit=Fit.CENTRAL, reason="r", opinions={}
    )
    await in_write(db, verdicts.add, found)  # and no settle
    assert await passes(db) == []
    monkeypatch.setattr(fulltext, "archive_for", lambda config: None)  # queued, not worked
    await nightly.run(gate, CONFIG, fetch=False, gateway=Writer().gateway)
    (row,) = await passes(db)
    assert (row["kind"], row["reason"], row["state"]) == ("brief", "focus", "awaiting_text")


async def test_the_nightly_reconcile_is_not_cut_short_by_the_limit(db: Session, gate, monkeypatch):
    await analysed(db, "solid")
    await graded(db, "central")
    stuck = "UPDATE fulltext_passes SET state = 'failed', failed_runs = %s WHERE kind <> 'deep'"
    await db.execute(stuck, (FAILED_RUNS,))
    other = paper("2610.00002", version=1)  # after P, which waits for the owner
    await in_write(db, papers_store.upsert, [other])
    await db.execute(
        "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
        "primary_class, model) SELECT %s, paper_revision, taxonomy_revision, primary_class, model "
        "FROM classifications WHERE paper_id = %s",
        (other.id, P.id),
    )
    await analysed(db, "solid", other.id)  # nothing left for the night's other steps
    found = Verdict(
        paper_id=other.id,
        topic=FAITH.slug,
        topic_version="v",
        fit=Fit.CENTRAL,
        reason="r",
        opinions={},
    )
    await in_write(db, verdicts.add, found)  # and no settle
    monkeypatch.setattr(fulltext, "archive_for", lambda config: None)
    await nightly.run(gate, CONFIG, fetch=False, limit=1, gateway=Writer().gateway)
    (row,) = await (
        await db.execute(
            "SELECT * FROM fulltext_passes WHERE paper_id = %s AND kind <> 'deep'", (other.id,)
        )
    ).fetchall()
    assert (row["kind"], row["state"]) == ("brief", "awaiting_text")


# -- the lease and the limit -----------------------------------------------------------------------


async def test_a_lease_holds_a_pass_until_it_runs_out(db: Session):
    await graded(db, "central")
    (held,) = await in_write(db, fulltext_store.claim, LEASE)
    assert await in_write(db, fulltext_store.claim, LEASE) == []  # held
    await db.execute(
        "UPDATE fulltext_passes SET lease_until = now() - interval '1 second' WHERE kind <> 'deep'"
    )
    (again,) = await in_write(db, fulltext_store.claim, LEASE)  # its worker died: tried again
    assert again.id == held.id
    # The first worker, back late, records nothing over the second's.
    assert await record(db, fulltext.Attempt(held, error="late", counts=True)) is False
    assert (await passes(db))[0]["failed_runs"] == 0


async def test_the_limit_is_taken_in_sql_least_recently_tried_first(db: Session):
    await graded(db, "central")
    other = paper("2610.00002", version=1)
    await in_write(db, papers_store.upsert, [other])
    found = Verdict(
        paper_id=other.id,
        topic=FAITH.slug,
        topic_version="v",
        fit=Fit.CENTRAL,
        reason="r",
        opinions={},
    )
    await in_write(db, verdicts.add, found)
    await settling.settle(db, other.id, cause=Cause.VERDICT)
    fake = FakeArchive(status="pending")
    assert await work(db, fake, Writer(), limit=1) == {"awaiting_text": 1}
    assert await work(db, fake, Writer(), limit=1) == {"awaiting_text": 1}
    assert [j["paper_id"] for j in fake.jobs] == [P.id, other.id]  # each in turn


async def test_waiting_for_texts_keeps_to_the_limit_over_every_round(
    db: Session, gate, monkeypatch
):
    await graded(db, "central")  # P's text is still being fetched
    others = [paper(f"2610.0000{n}", title=f"Paper {n}", version=1) for n in (2, 3, 4)]
    await in_write(db, papers_store.upsert, others)
    for other in others:
        found = Verdict(
            paper_id=other.id,
            topic=FAITH.slug,
            topic_version="v",
            fit=Fit.CENTRAL,
            reason="r",
            opinions={},
        )
        await in_write(db, verdicts.add, found)
        await settling.settle(db, other.id, cause=Cause.VERDICT)

    class Fetching(FakeArchive):
        def handle(self, req):
            if req.url.path == "/v1/jobs" and json.loads(req.content)["paper_id"] == P.id:
                return httpx.Response(202, json={"id": 99, "status": "pending"})
            return super().handle(req)

    fake, writer = Fetching(), Writer()
    for other in others:
        fake.texts[other.id] = (1, BODY)
    monkeypatch.setattr(fulltext, "TICK_SECONDS", 0)
    done = await fulltext.work_waiting(
        gate, CONFIG, archive(fake), limit=2, wait=5, gateway=writer.gateway
    )
    assert done["briefs_written"] == 2 and len(writer.asked) == 2


# -- passes as work items (docs/plans/a2-work-gate.md, P2) -----------------------------------------


@pytest.fixture
def soon(monkeypatch):
    """A pass awaiting its text tried again within a tenth of a second, not half a minute."""
    for module in (fulltext_store, text_worker):
        monkeypatch.setattr(module, "TEXT_WAIT", timedelta(seconds=0.1))


def workers(gate, fake: FakeArchive, writer: Writer) -> dict:
    return {"fulltext": TextPassWorker(gate, CONFIG, archive(fake), gateway=writer.gateway)}


async def state_is(conn: Session, state: str) -> bool:
    return [r["state"] for r in await passes(conn)] == [state]


async def item_ended(conn: Session, pass_id: int) -> bool:
    """The pass's item ended: its try is over, not only its pass's write."""
    return [i["state"] for i in await items_of(conn, "fulltext", str(pass_id))] == ["ended"]


async def test_a_pass_awaiting_its_text_runs_as_one_item_once_the_text_is_there(
    db: Session, gate, soon
):
    fake, writer = FakeArchive(status="pending"), Writer()
    async with serving(WorkGate(gate), workers(gate, fake, writer)):
        await graded(db, "central")
        (row,) = await passes(db)

        async def tried() -> bool:  # the archive asked, and the pass left awaiting its text
            return bool(fake.jobs) and await state_is(db, "awaiting_text")

        await until(tried, "the pass is tried and awaits its text")
        fake.status = "complete"
        ready(fake)
        await until(lambda: item_ended(db, row["id"]), "the item ends")
    assert await state_is(db, "done")
    (item,) = await items_of(db, "fulltext", str(row["id"]))
    assert item["attempts"] >= 2
    assert len(writer.asked) == 1


async def test_a_pass_the_nightly_worked_inline_leaves_its_item_nothing_to_do(db: Session, gate):
    await graded(db, "central")
    fake, writer = ready(FakeArchive()), Writer()
    assert await work(db, fake, writer) == {"briefs_written": 1}
    (row,) = await passes(db)
    async with serving(WorkGate(gate), workers(gate, fake, writer)):
        await until(lambda: item_ended(db, row["id"]), "the item ends")
    assert len(writer.asked) == 1 and len(fake.jobs) == 1  # nothing worked twice
    assert (await passes(db))[0]["state"] == "done"


async def test_a_claim_a_process_abandoned_is_taken_by_the_passs_item_once_it_lapses(
    db: Session, gate
):
    await graded(db, "central")
    (row,) = await passes(db)
    async with gate_on(db) as gone, gone.write("claim and die") as unit:
        (taken,) = await fulltext_store.claim(unit, timedelta(seconds=1), 1, pass_id=row["id"])
    fake, writer = ready(FakeArchive()), Writer()
    async with serving(WorkGate(gate), workers(gate, fake, writer)):
        await until(lambda: item_ended(db, row["id"]), "the item ends")
    assert await state_is(db, "done")
    (item,) = await items_of(db, "fulltext", str(row["id"]))
    # One try found it held and waited for its lease to end; the next took it over.
    assert item["attempts"] == 2
    assert item["not_before"] >= taken.lease_until and len(writer.asked) == 1


async def test_the_owners_run_now_runs_a_stopped_pass_while_the_gate_serves(db: Session, gate):
    await graded(db, "central")
    fake, writer = FakeArchive(status="failed"), Writer()
    await work(db, fake, writer)
    await db.execute("UPDATE fulltext_passes SET failed_runs = %s", (FAILED_RUNS,))
    await db.execute("DELETE FROM work_items")  # a stopped pass has none; the owner gives it one
    (row,) = await passes(db)
    fake = ready(FakeArchive())
    async with serving(WorkGate(gate), workers(gate, fake, writer)):
        _, http = client(db)
        async with http:
            assert (await http.post(f"/api/settings/fulltext/{row['id']}/now")).status_code == 204
        await until(lambda: item_ended(db, row["id"]), "the owner's pass is tried")
    assert await state_is(db, "done") and len(writer.asked) == 1


async def test_text_passes_run_as_many_at_once_as_a_wave_did(db: Session, gate):
    other = paper("2610.00002", title="Another", abstract="Another.", version=1)
    await in_write(db, papers_store.upsert, [other])
    async with Bridge(db).write("queue two") as unit:
        ids = [
            await fulltext_store.queue(unit, pid, 1, PassKind.BRIEF, "focus", TEXT_PRIORITY)
            for pid in (P.id, other.id)
        ]
    assert ids[0] % TEXT_WAVE != ids[1] % TEXT_WAVE  # two of the wave's slots
    running: set[str] = set()
    both = asyncio.Event()

    async def worker(item) -> None:  # holds its item until the other one runs too
        running.add(item.key)
        if len(running) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), WAIT)

    async with serving(WorkGate(gate), {"fulltext": worker}):
        await until(lambda: _ended_all(db, ids), "both items end")
    assert running == {str(i) for i in ids}


async def _ended_all(conn: Session, ids: list[int]) -> bool:
    items = [i for p in ids for i in await items_of(conn, "fulltext", str(p))]
    return len(items) == len(ids) and all(i["outcome"] == "done" for i in items)


# -- revisions -------------------------------------------------------------------------------------


async def test_a_new_arxiv_version_is_read_again_by_the_next_nightly_run(
    db: Session, gate, monkeypatch
):
    await analysed(db, "solid")  # nothing left for the night's other steps
    await graded(db, "central")
    await work(db, ready(FakeArchive()), Writer())
    # Stored by an intake that settles nothing after it: found from the database.
    assert (
        await in_write(db, papers_store.upsert, [P.model_copy(update={"version": 2})])
    ).rewritten == []
    assert await in_read(db, fulltext_store.outdated) == [P.id]
    fake = ready(FakeArchive(), version=2)
    monkeypatch.setattr(fulltext, "archive_for", lambda config: archive(fake))
    await nightly.run(gate, CONFIG, fetch=False, gateway=Writer().gateway)
    old, new = await passes(db)
    assert (old["arxiv_version"], new["state"], new["paper_revision"], new["arxiv_version"]) == (
        1,
        "done",
        1,
        2,
    )
    assert await in_read(db, fulltext_store.outdated) == []


@pytest.mark.parametrize("changed", [False, True])
async def test_a_new_version_with_the_same_text_is_answered_from_the_journal(db: Session, changed):
    await graded(db, "central")
    writer = Writer()
    await work(db, ready(FakeArchive()), writer)
    await in_write(db, papers_store.upsert, [P.model_copy(update={"version": 2})])
    await settling.settle_all(db, await in_read(db, fulltext_store.outdated), cause=Cause.VERDICT)
    fake = FakeArchive()
    fake.texts[P.id] = (2, BODY + " Revised." if changed else BODY)
    await work(db, fake, writer)
    old, new = await passes(db)
    assert (old["state"], new["state"], new["arxiv_version"]) == ("done", "done", 2)
    # The same text for the same revision is the same question: the journal answers it free.
    assert len(writer.asked) == (2 if changed else 1)
    calls = await (await db.execute("SELECT count(*) AS n FROM inference_calls")).fetchone()
    assert calls["n"] == (2 if changed else 1)


async def test_a_new_update_of_unknown_version_is_read_again_once(db: Session):
    await graded(db, "central")
    await work(db, ready(FakeArchive()), Writer())
    # Its raw history missing, intake knows an update came but not its version.
    later = P.model_copy(update={"version": None, "updated": "2026-10-20T00:00:00+00:00"})
    assert (await in_write(db, papers_store.upsert, [later])).rewritten == []
    assert await in_read(db, fulltext_store.outdated) == [P.id]
    await settling.settle_all(db, await in_read(db, fulltext_store.outdated), cause=Cause.VERDICT)
    assert [r["state"] for r in await passes(db)] == ["done", "awaiting_text"]
    fake = FakeArchive()
    fake.texts[P.id] = (None, BODY)  # the archive cannot say either: its historical LaTeX
    assert await work(db, fake, Writer()) == {"briefs_written": 1}
    assert await in_read(db, fulltext_store.outdated) == []  # once, not again
    await settling.settle(db, P.id, cause=Cause.VERDICT)
    assert len(await passes(db)) == 2
    # The archive then says which version it was: the version the first pass read, not the
    # one the last pass read, which is unknown, so it is read again.
    known = later.model_copy(update={"version": 1})
    await in_write(db, papers_store.upsert, [known], source="archive")
    assert await in_read(db, fulltext_store.outdated) == [P.id]


async def test_a_pass_that_finishes_after_a_rewrite_is_kept_under_the_old_revision(db: Session):
    await graded(db, "central")
    (claimed,) = await in_write(db, fulltext_store.claim, LEASE)
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})
    await settling.settle_all(
        db, (await in_write(db, papers_store.upsert, [rewrite])).rewritten, cause=Cause.VERDICT
    )
    # The running pass is kept; the old verdict still stands, so revision 2 has one queued.
    assert [(r["paper_revision"], r["state"]) for r in await passes(db)] == [
        (1, "running"),
        (2, "awaiting_text"),
    ]

    answer = brief.Output.model_validate(ANSWER)
    text = PaperText(BODY, 1, "latex")
    done = fulltext.Attempt(claimed, text=text, output=answer, model="test-brief")
    assert await record(db, done)
    (b,) = await (await db.execute("SELECT paper_revision FROM briefs")).fetchall()
    assert b["paper_revision"] == 1
    assert await in_read(db, analyses_store.weight_of, P.id) is None  # not revision 2's Weight
    assert await current_gist(db) is None
    assert await events(db, "gist.written") == []  # not the current text's
    assert [e["revision"] for e in await events(db, "brief.written")] == [1]
    assert [(r["paper_revision"], r["state"]) for r in await passes(db)] == [
        (1, "done"),
        (2, "awaiting_text"),
    ]


async def test_a_rewrite_whose_settle_was_cut_short_is_settled_by_the_nightly_sweep(db: Session):
    await graded(db, "central")  # its first pass awaits text
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})
    assert (await in_write(db, papers_store.upsert, [rewrite])).rewritten == [P.id]  # and no settle
    assert await in_read(db, fulltext_store.outdated) == [P.id]
    await settling.settle_all(db, await in_read(db, fulltext_store.outdated), cause=Cause.VERDICT)
    assert [(r["paper_revision"], r["state"]) for r in await passes(db)] == [(2, "awaiting_text")]
    assert await in_read(db, fulltext_store.outdated) == []


async def test_an_owners_fetch_over_an_older_version_settles_its_rewrite(db: Session):
    # Process or a miss fetched a newer version than litwatch holds (it stored the paper
    # meanwhile): stored as intake stores it, and its rewrite settled with it (L5-2, #66).
    await graded(db, "central")
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})
    stored, inserted = await settling.keep(db, P.id, rewrite)
    assert (stored.version, stored.title, inserted) == (2, rewrite.title, False)
    assert await in_read(db, fulltext_store.outdated) == []
    assert [(r["paper_revision"], r["state"]) for r in await passes(db)] == [(2, "awaiting_text")]


async def test_a_pass_that_finishes_after_its_paper_is_dismissed_writes_nothing(db: Session):
    await graded(db, "central")
    (claimed,) = await in_write(db, fulltext_store.claim, LEASE)
    assert await in_write(db, papers_store.dismiss, P.id)
    answer = brief.Output.model_validate(ANSWER)
    text = PaperText(BODY, 1, "latex")
    done = fulltext.Attempt(claimed, text=text, output=answer, model="test-brief")
    assert await record(db, done)
    assert await (await db.execute("SELECT * FROM briefs")).fetchall() == []
    assert await in_read(db, analyses_store.weight_of, P.id) is None
    assert await events(db, "brief.written", "gist.written") == []
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"]) == ("awaiting_text", 0)  # left for the owner's word
    assert await in_write(db, fulltext_store.claim, LEASE) == []


async def test_a_pass_not_started_before_a_rewrite_is_dropped(db: Session):
    await graded(db, "central")
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})
    await settling.settle_all(
        db, (await in_write(db, papers_store.upsert, [rewrite])).rewritten, cause=Cause.VERDICT
    )
    # The pass for revision 1 is gone; the old verdict still stands, so revision 2 has one.
    (row,) = await passes(db)
    assert row["paper_revision"] == 2


async def test_the_paper_page_shows_the_brief_labelled_with_its_revision(db: Session):
    await graded(db, "central")
    await work(db, ready(FakeArchive()), Writer())
    _, http = client(db)
    async with http:
        page = (await http.get(f"/api/papers/{P.id}")).json()
    assert (page["brief"]["text"], page["brief"]["revision"]) == (BRIEF, 1)
    assert page["gist"] == {"text": GIST, "source": "full_text"}
    assert page["weight"]["source"] == "full_text"
    assert (page["text_pass"]["kind"], page["text_pass"]["state"]) == ("brief", "done")


# -- the call --------------------------------------------------------------------------------------


def test_an_overstatement_is_one_line_saying_so_and_lengths_are_guarded():
    line = "The abstract overstates:\n the gain holds only for 7B models."
    out = brief.Output.model_validate(ANSWER | {"overstatement": line})
    assert out.overstatement == "The abstract overstates: the gain holds only for 7B models."
    assert brief.Output.model_validate(ANSWER | {"overstatement": " "}).overstatement is None
    for wrong in (
        {"overstatement": "The gain holds only for 7B models."},
        {"brief": "Too short."},
        {"gist": "Too short."},
    ):
        with pytest.raises(ValueError):
            brief.Output.model_validate(ANSWER | wrong)


def test_a_brief_keeps_its_paragraphs_up_to_four():
    first, second = " ".join(BRIEF.split()[:70]), " ".join(BRIEF.split()[70:])
    wrapped = first.replace(" layer ", " layer\n", 1)  # a line break inside a paragraph
    out = brief.Output.model_validate(ANSWER | {"brief": f" {wrapped} \n\n\n {second}\n"})
    assert out.brief == f"{first}\n\n{second}"
    five = "\n\n".join([first, second, "A third paragraph.", "A fourth.", "A fifth."])
    with pytest.raises(ValueError, match="5 paragraphs"):
        brief.Output.model_validate(ANSWER | {"brief": five})


def test_a_long_text_is_cut_and_an_unknown_area_said():
    request = brief.request(P, None, "x" * (brief.MAX_TEXT_CHARS + 10), "pdf")
    assert request.user.endswith(brief.CUT)
    assert "Area: not known" in request.user and "(from its PDF)" in request.user


LATEX = r"""\section{Results}
Probes reach 0.98 AUROC \citep{a}.
\begin{thebibliography}{2}
\bibitem[A(2025)]{a} A.~Author. \newblock A paper. \newblock 2025.
\end{thebibliography}

\appendix
\section{Proofs}
Lemma 1 holds."""


def test_a_pass_sends_latex_without_its_bibliography_and_pdf_text_as_read():
    sent = trim.without_references(LATEX, "latex")
    assert "0.98 AUROC" in sent and "Lemma 1 holds." in sent and r"\bibitem" not in sent
    assert brief.request(P, None, LATEX, "latex").user.endswith(sent)
    pdf = "Probes reach 0.98 AUROC.\nReferences\n[1] A. Author.\nAdditional Experiments\nMore."
    assert trim.without_references(pdf, "pdf") == pdf
    # The P1 variant drops LaTeX's appendix as well.
    assert "Lemma 1" not in trim.without_appendix(LATEX, "latex")


def test_a_text_without_a_bibliography_is_sent_as_read():
    for source in ("latex", "pdf"):
        assert trim.without_references(BODY, source) == BODY


def test_the_fulltext_command_finds_an_archive_kept_in_dotenv(tmp_path, monkeypatch):
    from typer.testing import CliRunner  # noqa: PLC0415

    from litwatch import cli  # noqa: PLC0415

    for name in ("LITWATCH_ARCHIVE_URL", "LITWATCH_ARCHIVE_TOKEN", "LITWATCH_CONFIG"):
        monkeypatch.setenv(name, "")  # recorded, so the undo removes what .env sets
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "LITWATCH_ARCHIVE_URL=http://127.0.0.1:9\nLITWATCH_ARCHIVE_TOKEN=t\n"
    )
    monkeypatch.setattr(cli, "_run", lambda work, **_: {})  # no database: only the archive matters
    result = CliRunner().invoke(cli.app, ["fulltext"])
    assert result.exit_code == 0, result.output
