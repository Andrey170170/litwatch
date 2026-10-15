"""The full-text queue: which papers get a pass and when (the owner's word included), working a
pass against the archive and the `brief` role, what it writes and settles, its failures, the
lease, revisions, and what the owner sees of it."""

import json

import httpx
import pytest

from litwatch import fulltext
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
from litwatch.core.fulltext import LEASE, PassKind, PassState, TextPass, to_queue, wanted
from litwatch.feedback import word
from litwatch.fulltext import brief
from litwatch.jobs import nightly
from litwatch.llm import Gateway, ProviderError
from litwatch.store import analyses as analyses_store
from litwatch.store import fulltext as fulltext_store
from litwatch.store import papers as papers_store
from litwatch.store import runs
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts
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


async def analysed(conn, weight: str) -> None:
    found = Analysis(
        gist="An abstract Gist of the paper.",
        weight=Weight(weight),
        weight_reason="From the abstract.",
        facets=Facets(
            contribution=Contribution.ANALYSIS, releases_code=False, releases_weights=False
        ),
    )
    current = await papers_store.get(conn, P.id)
    await analyses_store.add(conn, current, "interpretability", found, "m")
    await word.settle(conn, P.id)


async def passes(conn) -> list[dict]:
    return await (
        await conn.execute("SELECT * FROM fulltext_passes WHERE paper_id = %s ORDER BY id", (P.id,))
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
    done = TextPass(1, PassKind.BRIEF, PassState.DONE, 2)
    assert to_queue(want, None, 2)
    assert not to_queue(want, done, 2)
    assert to_queue(want, done, 3)  # a new arXiv version
    assert not to_queue(want, TextPass(1, PassKind.BRIEF, PassState.FAILED, None), 3)


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
    await graded(db, "central")
    assert len(await passes(db)) == 1
    tree = await taxonomy_store.load(db)
    faith = next(t for t in tree.topics if t.slug == FAITH.slug)
    await word.save_taxonomy(db, tree.following(faith.model_copy(update={"status": Status.PAUSED})))
    assert await passes(db) == []


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


async def test_a_pass_stopped_after_three_runs_waits_for_the_owner_who_runs_it_now(db):
    await graded(db, "central")
    fake, writer = FakeArchive(status="failed"), Writer()
    for _ in range(FAILED_RUNS):
        await fulltext_store.retry_failed(db, FAILED_RUNS)
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


async def test_a_pass_awaiting_text_over_a_week_waits_for_the_owner(db):
    await graded(db, "central")
    assert await fulltext_store.count_stuck(db, FAILED_RUNS) == 0
    await db.execute("UPDATE fulltext_passes SET queued_at = now() - interval '8 days'")
    (listed,) = await fulltext_store.stuck(db, FAILED_RUNS)
    assert listed["state"] == "awaiting_text"
    assert await fulltext_store.count_stuck(db, FAILED_RUNS) == 1


async def test_the_nightly_run_retries_failed_passes_and_works_the_queue(db, monkeypatch):
    await analysed(db, "solid")  # nothing left for the night's other steps
    await graded(db, "central")
    await db.execute("UPDATE fulltext_passes SET state = 'failed', failed_runs = 1")
    fake = ready(FakeArchive())
    monkeypatch.setattr(fulltext, "archive_for", lambda config: archive(fake))
    run_id = await nightly.run(db, CONFIG, fetch=False, gateway=Writer().gateway)
    counts = (await runs.get(db, run_id))["counts"]
    assert (counts["fulltext_retried"], counts["briefs_written"]) == (1, 1)
    assert counts["calls"]["brief"]["calls"] == 1  # on the nightly run
    assert (await passes(db))[0]["state"] == "done"


# -- the lease and the limit -----------------------------------------------------------------------


async def test_a_lease_holds_a_pass_until_it_runs_out(db):
    await graded(db, "central")
    (held,) = await fulltext_store.claim(db, LEASE)
    assert await fulltext_store.claim(db, LEASE) == []  # held
    await db.execute("UPDATE fulltext_passes SET lease_until = now() - interval '1 second'")
    (again,) = await fulltext_store.claim(db, LEASE)  # its worker died: tried again
    assert again.id == held.id
    # The first worker, back late, records nothing over the second's.
    assert await fulltext.record(db, fulltext.Attempt(held, error="late", counts=True)) is False
    assert (await passes(db))[0]["failed_runs"] == 0


async def test_the_limit_is_taken_in_sql_least_recently_tried_first(db):
    await graded(db, "central")
    other = paper("2610.00002", version=1)
    await papers_store.upsert(db, [other])
    await fulltext_store.queue(db, other.id, 1, PassKind.BRIEF, "focus", 0)
    fake = FakeArchive(status="pending")
    assert await work(db, fake, Writer(), limit=1) == {"awaiting_text": 1}
    assert await work(db, fake, Writer(), limit=1) == {"awaiting_text": 1}
    assert [j["paper_id"] for j in fake.jobs] == [P.id, other.id]  # each in turn


# -- revisions -------------------------------------------------------------------------------------


async def test_a_new_arxiv_version_queues_the_pass_again(db):
    await graded(db, "central")
    await work(db, ready(FakeArchive()), Writer())
    revised = await papers_store.upsert(db, [P.model_copy(update={"version": 2})])
    assert (revised.revised, revised.rewritten) == ([P.id], [])
    await word.settle_all(db, revised.revised)
    old, new = await passes(db)
    assert (old["state"], new["state"], new["paper_revision"]) == ("done", "awaiting_text", 1)
    await work(db, ready(FakeArchive(), version=2), Writer())
    assert (await passes(db))[1]["arxiv_version"] == 2


async def test_a_pass_that_finishes_after_a_rewrite_is_kept_under_the_old_revision(db):
    await graded(db, "central")
    (claimed,) = await fulltext_store.claim(db, LEASE)
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})
    await word.settle_all(db, (await papers_store.upsert(db, [rewrite])).revised)
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


async def test_a_pass_not_started_before_a_rewrite_is_dropped(db):
    await graded(db, "central")
    rewrite = P.model_copy(update={"version": 2, "title": "Entirely different words now"})
    await word.settle_all(db, (await papers_store.upsert(db, [rewrite])).revised)
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
