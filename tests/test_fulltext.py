"""The full-text queue: which papers get a pass and when (the owner's word included), working a
pass against the archive and the `brief` role, what it writes and settles, its failures, the
lease, revisions, and what the owner sees of it."""

import asyncio
import json

import httpx
import pytest

from litwatch import fulltext, store
from litwatch.api import create_app
from litwatch.archive import URGENT, ArchiveClient, PaperText
from litwatch.config import Config, Models, RoleConfig
from litwatch.core import (
    FAILED_RUNS,
    Analysis,
    Contribution,
    Facets,
    Fit,
    Label,
    LabelOrigin,
    LabelValue,
    Status,
    Verdict,
    Weight,
)
from litwatch.core.fulltext import (
    LEASE,
    RECONCILE_LOOKBACK,
    PassKind,
    PassState,
    TextPass,
    to_queue,
    to_relabel,
    wanted,
)
from litwatch.feedback import misses, word
from litwatch.fulltext import brief, trim
from litwatch.jobs import nightly
from litwatch.llm import Gateway, ProviderError
from litwatch.store import analyses as analyses_store
from litwatch.store import fulltext as fulltext_store
from litwatch.store import papers as papers_store
from litwatch.store import runs, verdicts
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import FAITH, INTERP, TREE, paper
from tests.fakes import ScriptedProvider
from tests.fakes_http import FakeArchive, mock

P = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes.", version=1)
BODY = "\\section{Method} We train a linear probe on every layer and read hidden steps. " * 40
GIST = (
    "Linear probes on every layer recover hidden reasoning steps, and traces omit about a third "
    "of them on four tasks."
)
BRIEF = " ".join(["Probes trained on every layer recover the hidden steps of a computation."] * 11)
ANSWER = {
    "overstatement": None,
    "brief": BRIEF,
    "gist": GIST,
    "weight_reason": "A careful analysis with a fair comparison.",
    "weight": "landmark",
}
BRIEF_ROLE = RoleConfig(provider="anthropic", model="test-brief")
CONFIG = Config(models=Models(brief=BRIEF_ROLE))
TOKEN = "t"


class Writer:
    """The `brief` role, scripted: `answers` in turn (an exception is raised), then ANSWER."""

    def __init__(self) -> None:
        self.answers: list[dict | Exception | str] = []
        self.asked: list[str] = []  # each request's user message

    def respond(self, role: RoleConfig, request) -> str:
        assert role.model == BRIEF_ROLE.model
        self.asked.append(request.user)
        answer = self.answers.pop(0) if self.answers else ANSWER
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)

    def gateway(self, config: Config, journal) -> Gateway:
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=(0.0,))


def archive(fake: FakeArchive) -> ArchiveClient:
    return ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))


@pytest.fixture
async def db(conn):
    """P classified, its primary class Interpretability (an Area, over the CoT faithfulness
    Focus topic)."""
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P])
    await conn.execute(
        "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
        "primary_class, model) VALUES (%s, 1, 'x', 'interpretability', 'm')",
        (P.id,),
    )
    return conn


def client(conn) -> tuple:
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, CONFIG)
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    return app, http


async def graded(conn, fit: str, topic: str = FAITH.slug) -> None:
    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id, topic=topic, topic_version="v", fit=Fit(fit), reason="r", opinions={}
        ),
    )
    await word.settle(conn, P.id)


async def analysed(conn, weight: str, paper_id: str = P.id) -> None:
    found = Analysis(
        gist="An abstract Gist of the paper.",
        weight=Weight(weight),
        weight_reason="From the abstract.",
        facets=Facets(
            contribution=Contribution.ANALYSIS, releases_code=False, releases_weights=False
        ),
    )
    current = await papers_store.get(conn, paper_id)
    await analyses_store.add(conn, current, "interpretability", found, "m")
    await word.settle(conn, paper_id)


async def passes(conn) -> list[dict]:
    """P's text passes; its Deep card passes are tests/test_deep.py's."""
    return await (
        await conn.execute(
            "SELECT * FROM fulltext_passes WHERE paper_id = %s AND kind <> 'deep' ORDER BY id",
            (P.id,),
        )
    ).fetchall()


async def events(conn, *types: str) -> list[dict]:
    rows = await (
        await conn.execute(
            "SELECT type, payload FROM outbox WHERE type = ANY(%s) ORDER BY id", (list(types),)
        )
    ).fetchall()
    return [{"type": r["type"], **r["payload"]} for r in rows]


async def work(conn, fake: FakeArchive, writer: Writer, **kwargs):
    return await fulltext.work(conn, CONFIG, archive(fake), gateway=writer.gateway, **kwargs)


def ready(fake: FakeArchive, version: int = 1) -> FakeArchive:
    fake.texts[P.id] = (version, BODY)
    return fake


async def current_gist(conn) -> str | None:
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


async def test_a_focus_paper_at_central_or_related_gets_a_brief_and_adjacent_none(db):
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


async def test_an_area_must_read_gets_a_weight_re_check(db):
    await analysed(db, "incremental")
    await graded(db, "central", INTERP.slug)  # central and incremental: relevant
    assert await passes(db) == []
    await analysed(db, "solid")  # central and solid: must read
    (row,) = await passes(db)
    assert (row["kind"], row["reason"]) == ("weight", "area")
    await graded(db, "related")  # now a Focus paper too: the pass not started says so
    (row,) = await passes(db)
    assert (row["kind"], row["reason"]) == ("brief", "focus")


async def test_the_owners_word_queues_a_pass_and_takes_back_one_not_started(db):
    await graded(db, "adjacent")
    said = Label(
        paper_id=P.id, topic=FAITH.slug, value=LabelValue.CENTRAL, origin=LabelOrigin.READER
    )
    await word.label(db, said)
    assert [r["kind"] for r in await passes(db)] == ["brief"]
    await word.label(db, said.model_copy(update={"value": LabelValue.ADJACENT}))
    assert await passes(db) == []  # not started and no longer wanted: dropped


async def test_a_paused_topic_queues_nothing_and_drops_its_pass(db):
    await graded(db, "related")  # relevant: the Area above would not pick it
    assert len(await passes(db)) == 1
    tree = await taxonomy_store.load(db)
    faith = next(t for t in tree.topics if t.slug == FAITH.slug)
    await word.save_taxonomy(db, tree.following(faith.model_copy(update={"status": Status.PAUSED})))
    assert await passes(db) == []


async def test_an_area_counts_a_must_read_its_descendant_graded(db):
    async def status(slug: str, value: Status) -> None:
        tree = await taxonomy_store.load(db)
        topic = next(t for t in tree.topics if t.slug == slug)
        await word.save_taxonomy(db, tree.following(topic.model_copy(update={"status": value})))

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
    tree = await taxonomy_store.load(db)
    moved = [
        c.model_copy(update={"parent": None}) if c.slug == FAITH.slug else c for c in tree.classes
    ]
    await word.save_taxonomy(db, tree.model_copy(update={"classes": moved}))
    assert await passes(db) == []


@pytest.mark.parametrize(
    "attempt",
    [{"waiting": True}, {"error": "no text", "counts": True}],
    ids=["still-fetching", "failed"],
)
async def test_a_pass_running_when_its_topic_is_paused_is_dropped_once_it_stops(db, attempt):
    await graded(db, "related")
    (claimed,) = await fulltext_store.claim(db, LEASE)
    tree = await taxonomy_store.load(db)
    faith = next(t for t in tree.topics if t.slug == FAITH.slug)
    await word.save_taxonomy(db, tree.following(faith.model_copy(update={"status": Status.PAUSED})))
    assert len(await passes(db)) == 1  # running: left to finish
    assert await fulltext.record(db, fulltext.Attempt(claimed, **attempt))
    assert await passes(db) == []  # back in the queue, no longer wanted: dropped


# -- working a pass --------------------------------------------------------------------------------


async def test_a_pass_awaits_its_text_then_writes_brief_gist_and_weight(db):
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
    assert (await analyses_store.weight_of(db, P.id))["source"] == "full_text"
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


async def test_an_abstract_gist_written_later_does_not_replace_the_full_texts(db):
    await graded(db, "central")
    await work(db, ready(FakeArchive()), Writer())
    await analysed(db, "incremental")  # an analysis that ran late: stored, not current
    assert (await analyses_store.weight_of(db, P.id))["weight"] == "landmark"
    assert await current_gist(db) == GIST
    assert [e["source"] for e in await events(db, "gist.written")] == ["full_text"]


async def test_failures_count_only_without_text_or_a_usable_answer(db):
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
    await fulltext_store.retry_failed(db, FAILED_RUNS)
    await work(db, fake, writer)  # the provider did not answer: not counted
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"], row["arxiv_version"]) == ("failed", 1, 1)
    writer.answers = ["{not json"] * 2
    await fulltext_store.retry_failed(db, FAILED_RUNS)
    await work(db, fake, writer)  # an invalid answer counts
    assert (await passes(db))[0]["failed_runs"] == 2


async def test_a_pass_tried_again_has_the_archive_try_again_what_it_gave_up_on(db):
    await graded(db, "central")
    fake = FakeArchive(status="failed")
    assert await work(db, fake, Writer()) == {"fulltext_failed": 1}
    assert fake.retried == []  # a first try takes the archive's word
    await fulltext_store.retry_failed(db, FAILED_RUNS)
    await fulltext_store.claim(db, LEASE)  # taken by a worker that died before reading
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


async def test_a_retry_covers_the_pdf_when_the_source_has_no_latex(db):
    await graded(db, "central")
    fake = FakeArchive()  # the source is downloaded, but has no LaTeX text
    fake.statuses["pdf"] = "failed"
    assert await work(db, fake, Writer()) == {"fulltext_failed": 1}
    await fulltext_store.retry_failed(db, FAILED_RUNS)
    assert await work(db, fake, Writer()) == {"awaiting_text": 1}
    assert [fake.jobs[i - 1]["kind"] for i in fake.retried] == ["pdf"]


async def test_a_pass_stopped_after_three_runs_waits_for_the_owner_who_runs_it_now(db):
    await graded(db, "central")
    fake, writer = FakeArchive(status="failed"), Writer()
    for _ in range(FAILED_RUNS):
        await fulltext_store.retry_failed(db, FAILED_RUNS)
        await work(db, fake, writer)  # tried again: the archive is asked to try again first
        fake.statuses.clear()  # which fails again
        await work(db, fake, writer)
    assert await fulltext_store.retry_failed(db, FAILED_RUNS) == 0  # no longer tried
    (row,) = await passes(db)

    await db.execute("INSERT INTO runs (kind, finished_at) VALUES ('nightly', now())")
    app, http = client(db)
    async with http:
        status = (await http.get("/api/status")).json()
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
    assert now.status_code == 204 and app.state.fulltext_wake.is_set()
    assert (again.status_code, missing.status_code) == (409, 404)  # no longer waiting; none
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"]) == ("ready", 0)
    await work(db, ready(FakeArchive()), writer)
    assert (await passes(db))[0]["state"] == "done"


async def test_use_the_pdf_runs_a_pass_waiting_for_the_owner_on_the_papers_pdf(db, monkeypatch):
    # DESIGN.md, Needs you: "Try again, Use the PDF" on a full-text failure.
    monkeypatch.setattr("litwatch.archive.text.pdf_to_markdown", lambda pdf: "From the PDF. " * 40)
    await graded(db, "central")
    fake, writer = FakeArchive(status="failed"), Writer()
    await work(db, fake, writer)
    await db.execute("UPDATE fulltext_passes SET failed_runs = %s", (FAILED_RUNS,))
    (row,) = await passes(db)
    app, http = client(db)
    async with http:
        used = await http.post(f"/api/settings/fulltext/{row['id']}/pdf")
        again = await http.post(f"/api/settings/fulltext/{row['id']}/pdf")
        missing = await http.post("/api/settings/fulltext/999999/pdf")
    assert used.status_code == 204 and app.state.fulltext_wake.is_set()
    assert (again.status_code, missing.status_code) == (409, 404)
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"], row["use_pdf"]) == ("ready", 0, True)
    fake = ready(FakeArchive())  # the LaTeX reads now, but the owner asked for the PDF
    assert await work(db, fake, writer) == {"briefs_written": 1}
    assert {j["kind"] for j in fake.jobs} == {"pdf"}  # its source never asked for
    (row,) = await passes(db)
    assert (row["state"], row["text_source"]) == ("done", "pdf")
    brief = await fulltext_store.brief_for(db, P.id)
    assert brief["source"] == "pdf"


async def test_a_pass_awaiting_text_over_a_week_waits_for_the_owner(db):
    await graded(db, "central")
    assert await fulltext_store.count_stuck(db, FAILED_RUNS) == 0
    await db.execute(
        "UPDATE fulltext_passes SET queued_at = now() - interval '8 days' WHERE kind <> 'deep'"
    )
    (listed,) = await fulltext_store.stuck(db, FAILED_RUNS)
    assert listed["state"] == "awaiting_text"
    assert await fulltext_store.count_stuck(db, FAILED_RUNS) == 1


async def test_the_nightly_run_retries_failed_passes_and_works_the_queue(db, monkeypatch):
    await analysed(db, "solid")  # nothing left for the night's other steps
    await graded(db, "central")
    await db.execute(
        "UPDATE fulltext_passes SET state = 'failed', failed_runs = 1 WHERE kind <> 'deep'"
    )
    fake = ready(FakeArchive())
    monkeypatch.setattr(fulltext, "archive_for", lambda config: archive(fake))
    run_id = await nightly.run(db, CONFIG, fetch=False, gateway=Writer().gateway)
    counts = (await runs.get(db, run_id))["counts"]
    assert (counts["fulltext_retried"], counts["briefs_written"]) == (1, 1)
    assert counts["calls"]["brief"]["calls"] == 1  # on the nightly run
    assert (await passes(db))[0]["state"] == "done"


async def test_failed_passes_are_retried_only_after_a_taxonomy_save_in_flight(db):
    await graded(db, "central")
    await db.execute(
        "UPDATE fulltext_passes SET state = 'failed', failed_runs = 1 WHERE kind <> 'deep'"
    )
    async with await store.connect(db.test_dsn, db.test_schema) as other:
        async with other.transaction():
            await taxonomy_store.lock(other)  # as a save that drops passes holds it
            retry = asyncio.create_task(fulltext_store.retry_failed(db, FAILED_RUNS))
            await asyncio.sleep(0.3)
            assert not retry.done()  # taxonomy first, as every settle: no deadlock
        assert await retry == 1


# -- the backstop: the database, not the triggers, decides -----------------------------------------


async def test_a_pass_its_paper_stopped_qualifying_for_is_dropped_at_claim_with_no_call(db):
    await graded(db, "central")
    await db.execute("UPDATE verdicts SET fit = 'none'")  # by a route that settles nothing
    fake, writer = ready(FakeArchive()), Writer()
    assert await work(db, fake, writer) == {"fulltext_dropped": 1}
    assert (writer.asked, fake.jobs, await passes(db)) == ([], [], [])


async def test_a_pass_wanted_for_another_reason_at_claim_runs_on_its_next_claim(db):
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


async def test_a_paper_dismissed_after_its_pass_is_claimed_spends_nothing(db, monkeypatch):
    await graded(db, "central")
    claim = fulltext_store.claim

    async def claim_then_dismiss(conn, *args):
        claimed = await claim(conn, *args)
        await papers_store.dismiss(conn, P.id)
        return claimed

    monkeypatch.setattr(fulltext_store, "claim", claim_then_dismiss)
    fake, writer = ready(FakeArchive()), Writer()
    assert await work(db, fake, writer) == {"fulltext_dismissed": 1}
    assert (writer.asked, fake.jobs) == ([], [])
    (row,) = await passes(db)
    assert row["state"] == "awaiting_text"  # left for the owner's word, as `record` leaves it


async def test_a_pass_whose_paper_was_rewritten_after_its_claim_is_dropped_unread(db, monkeypatch):
    await graded(db, "central")
    claim = fulltext_store.claim
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})

    async def claim_then_rewrite(conn, *args):
        claimed = await claim(conn, *args)
        await word.settle_all(conn, (await papers_store.upsert(conn, [rewrite])).rewritten)
        return claimed

    monkeypatch.setattr(fulltext_store, "claim", claim_then_rewrite)
    fake, writer = ready(FakeArchive()), Writer()
    assert await work(db, fake, writer, limit=1) == {"fulltext_dropped": 1}
    assert (writer.asked, fake.jobs) == ([], [])
    assert [(r["paper_revision"], r["state"]) for r in await passes(db)] == [(2, "awaiting_text")]


async def test_the_reconcile_leaves_out_a_paper_its_area_grades_below_must_read(db):
    await analysed(db, "solid")
    await graded(db, "central")  # must read in the Focus topic
    await graded(db, "adjacent", INTERP.slug)  # the Area's own grade stands over it
    tree = await taxonomy_store.load(db)
    faith = next(t for t in tree.topics if t.slug == FAITH.slug)
    await word.save_taxonomy(db, tree.following(faith.model_copy(update={"status": Status.PAUSED})))
    assert await passes(db) == []
    await db.execute("UPDATE verdicts SET recorded_at = now() - interval '3 days'")
    assert await fulltext_store.candidates(db, RECONCILE_LOOKBACK) == []


async def test_a_paper_that_came_to_qualify_by_no_trigger_gets_its_pass_on_the_nightly_run(
    db, monkeypatch
):
    await analysed(db, "solid")  # nothing left for the night's other steps
    found = Verdict(
        paper_id=P.id, topic=FAITH.slug, topic_version="v", fit=Fit.CENTRAL, reason="r", opinions={}
    )
    await verdicts.add(db, found)  # and no settle
    assert await passes(db) == []
    monkeypatch.setattr(fulltext, "archive_for", lambda config: None)  # queued, not worked
    await nightly.run(db, CONFIG, fetch=False, gateway=Writer().gateway)
    (row,) = await passes(db)
    assert (row["kind"], row["reason"], row["state"]) == ("brief", "focus", "awaiting_text")


async def test_the_nightly_reconcile_is_not_cut_short_by_the_limit(db, monkeypatch):
    await analysed(db, "solid")
    await graded(db, "central")
    stuck = "UPDATE fulltext_passes SET state = 'failed', failed_runs = %s WHERE kind <> 'deep'"
    await db.execute(stuck, (FAILED_RUNS,))
    other = paper("2610.00002", version=1)  # after P, which waits for the owner
    await papers_store.upsert(db, [other])
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
    await verdicts.add(db, found)  # and no settle
    monkeypatch.setattr(fulltext, "archive_for", lambda config: None)
    await nightly.run(db, CONFIG, fetch=False, limit=1, gateway=Writer().gateway)
    (row,) = await (
        await db.execute(
            "SELECT * FROM fulltext_passes WHERE paper_id = %s AND kind <> 'deep'", (other.id,)
        )
    ).fetchall()
    assert (row["kind"], row["state"]) == ("brief", "awaiting_text")


# -- the lease and the limit -----------------------------------------------------------------------


async def test_a_lease_holds_a_pass_until_it_runs_out(db):
    await graded(db, "central")
    (held,) = await fulltext_store.claim(db, LEASE)
    assert await fulltext_store.claim(db, LEASE) == []  # held
    await db.execute(
        "UPDATE fulltext_passes SET lease_until = now() - interval '1 second' WHERE kind <> 'deep'"
    )
    (again,) = await fulltext_store.claim(db, LEASE)  # its worker died: tried again
    assert again.id == held.id
    # The first worker, back late, records nothing over the second's.
    assert await fulltext.record(db, fulltext.Attempt(held, error="late", counts=True)) is False
    assert (await passes(db))[0]["failed_runs"] == 0


async def test_the_limit_is_taken_in_sql_least_recently_tried_first(db):
    await graded(db, "central")
    other = paper("2610.00002", version=1)
    await papers_store.upsert(db, [other])
    found = Verdict(
        paper_id=other.id,
        topic=FAITH.slug,
        topic_version="v",
        fit=Fit.CENTRAL,
        reason="r",
        opinions={},
    )
    await verdicts.add(db, found)
    await word.settle(db, other.id)
    fake = FakeArchive(status="pending")
    assert await work(db, fake, Writer(), limit=1) == {"awaiting_text": 1}
    assert await work(db, fake, Writer(), limit=1) == {"awaiting_text": 1}
    assert [j["paper_id"] for j in fake.jobs] == [P.id, other.id]  # each in turn


async def test_waiting_for_texts_keeps_to_the_limit_over_every_round(db, monkeypatch):
    await graded(db, "central")  # P's text is still being fetched
    others = [paper(f"2610.0000{n}", title=f"Paper {n}", version=1) for n in (2, 3, 4)]
    await papers_store.upsert(db, others)
    for other in others:
        found = Verdict(
            paper_id=other.id,
            topic=FAITH.slug,
            topic_version="v",
            fit=Fit.CENTRAL,
            reason="r",
            opinions={},
        )
        await verdicts.add(db, found)
        await word.settle(db, other.id)

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
        db, CONFIG, archive(fake), limit=2, wait=5, gateway=writer.gateway
    )
    assert done["briefs_written"] == 2 and len(writer.asked) == 2


# -- revisions -------------------------------------------------------------------------------------


async def test_a_new_arxiv_version_is_read_again_by_the_next_nightly_run(db, monkeypatch):
    await analysed(db, "solid")  # nothing left for the night's other steps
    await graded(db, "central")
    await work(db, ready(FakeArchive()), Writer())
    # Stored by an intake that settles nothing after it: found from the database.
    assert (await papers_store.upsert(db, [P.model_copy(update={"version": 2})])).rewritten == []
    assert await fulltext_store.outdated(db) == [P.id]
    fake = ready(FakeArchive(), version=2)
    monkeypatch.setattr(fulltext, "archive_for", lambda config: archive(fake))
    await nightly.run(db, CONFIG, fetch=False, gateway=Writer().gateway)
    old, new = await passes(db)
    assert (old["arxiv_version"], new["state"], new["paper_revision"], new["arxiv_version"]) == (
        1,
        "done",
        1,
        2,
    )
    assert await fulltext_store.outdated(db) == []


@pytest.mark.parametrize("changed", [False, True])
async def test_a_new_version_with_the_same_text_is_answered_from_the_journal(db, changed):
    await graded(db, "central")
    writer = Writer()
    await work(db, ready(FakeArchive()), writer)
    await papers_store.upsert(db, [P.model_copy(update={"version": 2})])
    await word.settle_all(db, await fulltext_store.outdated(db))
    fake = FakeArchive()
    fake.texts[P.id] = (2, BODY + " Revised." if changed else BODY)
    await work(db, fake, writer)
    old, new = await passes(db)
    assert (old["state"], new["state"], new["arxiv_version"]) == ("done", "done", 2)
    # The same text for the same revision is the same question: the journal answers it free.
    assert len(writer.asked) == (2 if changed else 1)
    calls = await (await db.execute("SELECT count(*) AS n FROM inference_calls")).fetchone()
    assert calls["n"] == (2 if changed else 1)


async def test_a_new_update_of_unknown_version_is_read_again_once(db):
    await graded(db, "central")
    await work(db, ready(FakeArchive()), Writer())
    # Its raw history missing, intake knows an update came but not its version.
    later = P.model_copy(update={"version": None, "updated": "2026-10-20T00:00:00+00:00"})
    assert (await papers_store.upsert(db, [later])).rewritten == []
    assert await fulltext_store.outdated(db) == [P.id]
    await word.settle_all(db, await fulltext_store.outdated(db))
    assert [r["state"] for r in await passes(db)] == ["done", "awaiting_text"]
    fake = FakeArchive()
    fake.texts[P.id] = (None, BODY)  # the archive cannot say either: its historical LaTeX
    assert await work(db, fake, Writer()) == {"briefs_written": 1}
    assert await fulltext_store.outdated(db) == []  # once, not again
    await word.settle(db, P.id)
    assert len(await passes(db)) == 2
    # The archive then says which version it was: the version the first pass read, not the
    # one the last pass read, which is unknown, so it is read again.
    known = later.model_copy(update={"version": 1})
    await papers_store.upsert(db, [known], source="archive")
    assert await fulltext_store.outdated(db) == [P.id]


async def test_a_pass_that_finishes_after_a_rewrite_is_kept_under_the_old_revision(db):
    await graded(db, "central")
    (claimed,) = await fulltext_store.claim(db, LEASE)
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})
    await word.settle_all(db, (await papers_store.upsert(db, [rewrite])).rewritten)
    # The running pass is kept; the old verdict still stands, so revision 2 has one queued.
    assert [(r["paper_revision"], r["state"]) for r in await passes(db)] == [
        (1, "running"),
        (2, "awaiting_text"),
    ]

    answer = brief.Output.model_validate(ANSWER)
    text = PaperText(BODY, 1, "latex")
    done = fulltext.Attempt(claimed, text=text, output=answer, model="test-brief")
    assert await fulltext.record(db, done)
    (b,) = await (await db.execute("SELECT paper_revision FROM briefs")).fetchall()
    assert b["paper_revision"] == 1
    assert await analyses_store.weight_of(db, P.id) is None  # not revision 2's Weight
    assert await current_gist(db) is None
    assert await events(db, "gist.written") == []  # not the current text's
    assert [e["revision"] for e in await events(db, "brief.written")] == [1]
    assert [(r["paper_revision"], r["state"]) for r in await passes(db)] == [
        (1, "done"),
        (2, "awaiting_text"),
    ]


async def test_a_rewrite_whose_settle_was_cut_short_is_settled_by_the_nightly_sweep(db):
    await graded(db, "central")  # its first pass awaits text
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})
    assert (await papers_store.upsert(db, [rewrite])).rewritten == [P.id]  # and no settle
    assert await fulltext_store.outdated(db) == [P.id]
    await word.settle_all(db, await fulltext_store.outdated(db))
    assert [(r["paper_revision"], r["state"]) for r in await passes(db)] == [(2, "awaiting_text")]
    assert await fulltext_store.outdated(db) == []


async def test_an_owners_fetch_over_an_older_version_settles_its_rewrite(db):
    # Process or a miss fetched a newer version than litwatch holds (it stored the paper
    # meanwhile): stored as intake stores it, and its rewrite settled with it (L5-2, #66).
    await graded(db, "central")
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})
    stored, inserted = await misses.keep(db, P.id, rewrite)
    assert (stored.version, stored.title, inserted) == (2, rewrite.title, False)
    assert await fulltext_store.outdated(db) == []
    assert [(r["paper_revision"], r["state"]) for r in await passes(db)] == [(2, "awaiting_text")]


async def test_a_pass_that_finishes_after_its_paper_is_dismissed_writes_nothing(db):
    await graded(db, "central")
    (claimed,) = await fulltext_store.claim(db, LEASE)
    assert await papers_store.dismiss(db, P.id)
    answer = brief.Output.model_validate(ANSWER)
    text = PaperText(BODY, 1, "latex")
    done = fulltext.Attempt(claimed, text=text, output=answer, model="test-brief")
    assert await fulltext.record(db, done)
    assert await (await db.execute("SELECT * FROM briefs")).fetchall() == []
    assert await analyses_store.weight_of(db, P.id) is None
    assert await events(db, "brief.written", "gist.written") == []
    (row,) = await passes(db)
    assert (row["state"], row["failed_runs"]) == ("awaiting_text", 0)  # left for the owner's word
    assert await fulltext_store.claim(db, LEASE) == []


async def test_a_pass_not_started_before_a_rewrite_is_dropped(db):
    await graded(db, "central")
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})
    await word.settle_all(db, (await papers_store.upsert(db, [rewrite])).rewritten)
    # The pass for revision 1 is gone; the old verdict still stands, so revision 2 has one.
    (row,) = await passes(db)
    assert row["paper_revision"] == 2


async def test_the_paper_page_shows_the_brief_labelled_with_its_revision(db):
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
