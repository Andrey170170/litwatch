"""Classification and analysis as nightly steps: what they record, their events, failures and
the owner's actions on a paper classification keeps failing on. Models are scripted; grading
answers too, and is tested in test_grading."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import ValidationError

from litwatch import classification
from litwatch.analysis import Output
from litwatch.api import create_app
from litwatch.api import settings as settings_api
from litwatch.config import Classification, Config, Models, RoleConfig
from litwatch.core import FAILED_RUNS, Paper
from litwatch.jobs import nightly
from litwatch.jobs.context import model_run, run_record
from litwatch.llm import Gateway, ProviderError, Usage
from litwatch.store import classifications as classifications_store
from litwatch.store import failures, runs
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store.calls import CallJournal
from tests.conftest import TREE, paper
from tests.fakes import ScriptedProvider

CLASSIFY = RoleConfig(provider="openrouter", model="test/decider")
# Each role its own model, so a scripted answer knows which role was asked.
ROLES = {
    "classify": CLASSIFY,
    "analyse": RoleConfig(provider="anthropic", model="test-analyse"),
    "judge": RoleConfig(provider="anthropic", model="test-judge"),
    "check": RoleConfig(provider="openrouter", model="test/check"),
    "area_check": RoleConfig(provider="openrouter", model="test/area-check"),
    "field_grade": RoleConfig(provider="anthropic", model="test-field"),
}
CONFIG = Config(models=Models(**ROLES))
BY_MODEL = {r.model: name for name, r in ROLES.items()}
NAMES = {c.slug: c.name for c in TREE.classes}
GIST = (
    "Probes read a model's hidden steps from its activations and find that traces omit a third "
    "of them across four tasks."
)
ANALYSIS = {
    "gist": GIST,
    "contribution": "analysis",
    "releases_code": True,
    "releases_weights": False,
    "weight_reason": "A careful analysis with a fair comparison.",
    "weight": "solid",
}

A = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes on activations.")
B = paper("2610.00002", title="Is the chain of thought faithful?", abstract="We test traces.")


def decision(primary: str = "other", **probabilities: float) -> dict:
    return (
        dict.fromkeys(NAMES, 0.0)
        | probabilities
        | {"primary_class": {"option": NAMES[primary], "probability": 0.9}}
    )


class Scripted:
    """Scripted roles, answering by the paper's title: `classify` and `analyse`, and the
    graders (`fits` by (role, title); related unless scripted, and `area_check` sure it holds)."""

    def __init__(self) -> None:
        self.decisions: dict[str, dict] = {}
        self.analyses: dict[str, dict] = {}
        self.fits: dict[tuple[str, str], dict | Exception] = {}
        self.asked: list[tuple[str, str]] = []  # (role, title)
        self.users: list[str] = []  # every request's user message

    def respond(self, role: RoleConfig, request) -> str:
        title = request.user.split("Title: ", 1)[1].split("\n", 1)[0]
        step = BY_MODEL[role.model]
        self.asked.append((step, title))
        self.users.append(request.user)
        match step:
            case "classify":
                answer = self.decisions.get(title) or decision()
            case "analyse":
                answer = self.analyses.get(title) or ANALYSIS
            case "area_check":
                answer = self.fits.get((step, title)) or {"holds": 0.9}
            case _:
                answer = self.fits.get((step, title)) or {
                    "reason": "On the subject.",
                    "fit": "related",
                }
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer)

    def gateway(self, config: Config, journal) -> Gateway:
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=(0.0,))


@pytest.fixture
async def models(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [A, B])
    return Scripted()


async def night(conn, models: Scripted, config: Config = CONFIG) -> dict:
    run_id = await nightly.run(conn, config, fetch=False, gateway=models.gateway)
    return (await runs.get(conn, run_id))["counts"]


async def rows(conn, query: str, *params) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def members(conn, paper_id: str) -> set[tuple[str, str]]:
    found = await classifications_store.memberships(conn, paper_id)
    return {(m.slug, m.source.value) for m in found}


async def test_thresholds_the_band_the_primary_class_and_the_keyword_check(conn, models):
    # Interpretability at the default 0.8; its Focus child CoT faithfulness in the band
    # [0.1, 0.5); Agents just under 0.8; the primary class whatever its own probability.
    models.decisions[A.title] = decision(
        "other", interpretability=0.85, **{"cot-faithfulness": 0.3}, agents=0.79
    )
    counts = await night(conn, models)
    assert counts["classified"] == 2
    assert await members(conn, A.id) == {
        ("interpretability", "classifier"),
        ("other", "classifier"),
        ("cot-faithfulness", "uncertain"),
    }
    # B: no probability anywhere, but its title matches a Focus topic's keyword.
    assert await members(conn, B.id) == {("other", "classifier"), ("cot-faithfulness", "keyword")}

    (row,) = await rows(conn, "SELECT * FROM classifications WHERE paper_id = %s", A.id)
    assert row["primary_class"] == "other" and row["paper_revision"] == 1
    assert row["response"]["probabilities"]["agents"] == 0.79  # every class's kept
    assert row["taxonomy_revision"] == TREE.classes_version
    (band,) = await rows(
        conn, "SELECT * FROM memberships WHERE paper_id = %s AND source = 'uncertain'", A.id
    )
    assert band["probability"] == 0.3
    assert band["class_version"] == TREE.get("cot-faithfulness").version

    (event,) = await rows(
        conn, "SELECT * FROM outbox WHERE type = 'paper.classified' AND paper_id = %s", A.id
    )
    assert event["payload"] == {
        "revision": 1,
        "primary": "other",
        "classes": ["cot-faithfulness", "interpretability", "other"],
        "by_owner": False,
    }


async def test_the_configured_thresholds_draw_membership(conn, models):
    models.decisions[A.title] = decision("other", interpretability=0.85)
    stricter = Config(models=CONFIG.models, classification=Classification(default_threshold=0.9))
    await night(conn, models, stricter)
    assert await members(conn, A.id) == {("other", "classifier")}


async def test_the_owners_class_label_outranks_the_classifier(conn, models):
    models.decisions[A.title] = decision("other", interpretability=0.95)
    version = TREE.get("interpretability").version
    await classifications_store.put_owner(conn, A, "interpretability", False, version)
    await classifications_store.put_owner(conn, A, "agents", True, TREE.get("agents").version)
    await night(conn, models)
    # The classifier's word is kept, but the owner's decides.
    assert ("interpretability", "classifier") in await members(conn, A.id)
    (event,) = await rows(
        conn, "SELECT * FROM outbox WHERE type = 'paper.classified' AND paper_id = %s", A.id
    )
    assert event["payload"]["classes"] == ["agents", "other"]


async def test_an_invalid_answer_fails_and_three_failed_runs_leave_the_paper_to_the_owner(
    conn, models
):
    models.decisions[A.title] = decision("other", interpretability=1.4)  # outside [0, 1]
    for n in range(1, FAILED_RUNS + 1):
        counts = await night(conn, models)
        assert counts["classification_failed"] == 1
        (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE paper_id = %s", A.id)
        assert failure["failed_runs"] == n and failure["last_error"].startswith("invalid")
    assert await rows(conn, "SELECT 1 FROM classifications WHERE paper_id = %s", A.id) == []
    assert await classifications_store.due(conn, FAILED_RUNS) == []
    models.asked.clear()
    await night(conn, models)
    assert ("classify", A.title) not in models.asked  # it waits for the owner now


async def test_a_rewrite_classifies_and_analyses_again_and_keeps_the_old_records(conn, models):
    await night(conn, models)
    rewritten = A.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(conn, [rewritten])
    models.decisions[A.title] = decision("agents", agents=0.9)
    counts = await night(conn, models)
    assert counts["classified"] == 1 and counts["analysed"] == 1
    query = "SELECT paper_revision, primary_class FROM {} WHERE paper_id = %s ORDER BY id"
    classified = await rows(conn, query.format("classifications"), A.id)
    assert classified == [
        {"paper_revision": 1, "primary_class": "other"},
        {"paper_revision": 2, "primary_class": "agents"},
    ]
    assert await members(conn, A.id) == {("agents", "classifier")}  # replaced, not added to
    analysed = await rows(conn, query.format("analyses"), A.id)
    assert analysed[-1] == {"paper_revision": 2, "primary_class": "agents"}


async def test_analysis_records_gist_weight_and_facets_with_its_event(conn, models):
    counts = await night(conn, models)
    assert counts["analysed"] == 2
    (a,) = await rows(conn, "SELECT * FROM analyses WHERE paper_id = %s", A.id)
    assert (a["gist"], a["weight"], a["contribution"]) == (GIST, "solid", "analysis")
    assert (a["releases_code"], a["releases_weights"], a["primary_class"]) == (True, False, "other")
    (w,) = await rows(conn, "SELECT * FROM weights WHERE paper_id = %s", A.id)
    assert (w["weight"], w["source"], w["reason"]) == (
        "solid",
        "abstract",
        ANALYSIS["weight_reason"],
    )
    (g,) = await rows(conn, "SELECT * FROM gists WHERE paper_id = %s", A.id)
    assert (g["text"], g["source"], g["paper_revision"]) == (GIST, "abstract", 1)
    (event,) = await rows(
        conn, "SELECT * FROM outbox WHERE type = 'gist.written' AND paper_id = %s", A.id
    )
    assert event["payload"] == {"revision": 1, "source": "abstract", "gist_id": g["id"]}
    # Weight is judged in the paper's primary class, named in the request.
    asked = [u for u in models.users if u.startswith("Area: ")]
    assert len(asked) == 2 and all(u.startswith("Area: Other: ") for u in asked)
    await night(conn, models)
    assert len(await rows(conn, "SELECT 1 FROM analyses")) == 2  # done once per revision


def test_a_gist_over_the_target_is_kept_and_only_a_broken_one_fails():
    # 38 words, from the two-week run (2610.02632): over the 30 asked for, still a Gist.
    long = (
        "Introduces OMVV, which routes each step among a pool of weak verifiers with differing "
        "cost via exponential weights, giving distribution-free error guarantees and sublinear "
        "regret. It reaches higher accuracy at lower verification cost than any single fixed "
        "verifier."
    )
    assert len(long.split()) == 38
    assert Output.model_validate(ANALYSIS | {"gist": long}).gist == long
    for broken in ("Too short.", " ".join([GIST] * 3)):  # a fragment, an abstract's length
        with pytest.raises(ValidationError):
            Output.model_validate(ANALYSIS | {"gist": broken})


async def test_failed_analysis_stops_after_three_runs_until_the_owner_says_try_again(conn, models):
    models.analyses[A.title] = ANALYSIS | {"gist": "Too short."}  # not a Gist: invalid
    for _ in range(FAILED_RUNS):
        assert (await night(conn, models))["analysis_failed"] == 1
    (listed,) = await failures.stuck(conn, "analysis", FAILED_RUNS)
    assert listed["paper_id"] == A.id and listed["title"] == A.title
    del models.analyses[A.title]
    models.asked.clear()
    await night(conn, models)
    assert ("analyse", A.title) not in models.asked  # left for the owner, at no cost
    async with api(conn) as client:
        status = (await client.get("/api/status")).json()
        assert status["state"] == "attention" and status["analysis_failed"] == 1
        url = "/api/settings/analysis/{}/retry"
        assert (await client.post(url.format(A.id), json={"revision": 2})).status_code == 409
        assert (await client.post(url.format(B.id), json=AT_1)).status_code == 409  # analysed
        assert (await client.post(url.format(A.id), json=AT_1)).status_code == 204
    assert (await night(conn, models))["analysed"] == 1
    assert await failures.stuck(conn, "analysis", FAILED_RUNS) == []


async def test_a_result_for_text_that_changed_meanwhile_records_nothing(conn, models):
    tree = await taxonomy_store.load(conn)
    gateway = models.gateway(CONFIG, None)
    async with classification.classify([A], tree, gateway) as results:
        (found,) = [c async for c in results]
    rewritten = A.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(conn, [rewritten])
    assert not await classification.record(conn, found)
    assert await rows(conn, "SELECT 1 FROM classifications") == []
    assert await rows(conn, "SELECT 1 FROM outbox WHERE type = 'paper.classified'") == []


@pytest.mark.parametrize("step", ["classify", "analyse"])
@pytest.mark.parametrize(
    "error",
    [
        ProviderError("APITimeoutError", timeout=True),
        ProviderError("AuthenticationError: no key"),
    ],
)
async def test_a_model_that_did_not_answer_is_retried_without_counting(conn, models, step, error):
    (models.decisions if step == "classify" else models.analyses)[A.title] = error
    table = "classification" if step == "classify" else "analysis"
    for _ in range(FAILED_RUNS + 1):
        models.asked.clear()
        await night(conn, models)
        assert (step, A.title) in models.asked  # tried again every run
    (failure,) = await rows(conn, "SELECT * FROM step_failures WHERE paper_id = %s", A.id)
    assert (failure["step"], failure["failed_runs"]) == (table, 0)
    assert failure["last_error"].startswith(("timeout", "provider"))
    assert await failures.stuck(conn, table, FAILED_RUNS) == []


async def test_a_retry_that_did_not_count_still_comes_after_new_papers(conn, models):
    # A is older than B by id; a failure that adds no failed run still sends it to the back.
    await failures.failed(conn, A, "classification", "timeout: x", counts=False)
    assert [p.id for p in await classifications_store.due(conn, FAILED_RUNS, limit=1)] == [B.id]


async def test_a_failing_record_stops_every_call_before_the_run_closes(conn, models, monkeypatch):
    """B's call never returns; recording A fails. The run must not end with B still asking."""
    stalled, cancelled = asyncio.Event(), []

    class Stalling(ScriptedProvider):
        async def complete(self, role, request):
            if B.title not in request.user:
                return await super().complete(role, request)
            stalled.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(B.id)
                raise

    def gateway(config, journal):
        providers = {n: Stalling(n, models.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=(0.0,))

    async def boom(conn, found):
        await stalled.wait()
        raise RuntimeError("the database went away")

    monkeypatch.setattr(classification, "record", boom)
    with pytest.raises(RuntimeError):
        await nightly.run(conn, CONFIG, fetch=False, gateway=gateway)
    assert cancelled == [B.id]
    (run,) = await rows(conn, "SELECT error, finished_at FROM runs")
    assert run["finished_at"] and "the database went away" in run["error"]


@pytest.mark.parametrize(
    "failure", [RuntimeError("the journal's connection went away"), asyncio.CancelledError()]
)
async def test_a_journal_that_fails_after_the_job_keeps_the_jobs_error_and_its_cost(
    conn, monkeypatch, failure
):
    async def lost(self):
        raise failure

    monkeypatch.setattr(CallJournal, "by_role", lost)
    with pytest.raises(ValueError, match="the job's own"):
        async with model_run(conn, CONFIG, "nightly", gateway=lambda config, journal: None) as run:
            call = await run.journal.start("judge", "m", "key", None)
            await run.journal.record(call, Usage(model="m", cost=0.25))
            raise ValueError("the job's own")
    (run,) = await rows(conn, "SELECT error, cost, finished_at FROM runs")
    assert run["finished_at"] and run["error"] == "ValueError: the job's own"
    assert float(run["cost"]) == 0.25  # read before the count by role that failed


async def test_a_run_that_cannot_close_after_the_job_failed_raises_the_jobs_error(
    conn, monkeypatch
):
    async def unclosable(*args, **kwargs):
        raise RuntimeError("could not close")

    monkeypatch.setattr(runs, "finish", unclosable)
    with pytest.raises(ValueError, match="the job's own"):
        async with run_record(conn, "intake"):
            raise ValueError("the job's own")
    with pytest.raises(RuntimeError, match="could not close"):  # nothing else to report
        async with run_record(conn, "intake"):
            pass


# -- the owner's three actions on a paper awaiting classification --------------------------------

TOKEN = "s3cret"
AT_1 = {"revision": 1}  # the revision Quality listed the paper at


@pytest.fixture
async def client(conn, models):
    models.decisions[A.title] = {"broken": True}  # never valid
    for _ in range(FAILED_RUNS):
        await night(conn, models)
    async with api(conn) as c:
        yield c


@asynccontextmanager
async def api(conn) -> AsyncIterator[httpx.AsyncClient]:
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    transport = httpx.ASGITransport(app=create_app(dsn, TOKEN, CONFIG))
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def awaiting(client) -> list[str]:
    quality = (await client.get("/api/settings/quality")).json()
    return [r["paper_id"] for r in quality["night"]["awaiting_classification"]]


async def test_a_paper_awaiting_classification_is_counted_and_listed(client):
    status = (await client.get("/api/status")).json()
    assert status["state"] == "attention" and status["awaiting_classification"] == 1
    assert await awaiting(client) == [A.id]
    quality = (await client.get("/api/settings/quality")).json()
    assert quality["night"]["awaiting_classification"][0]["revision"] == 1
    url = "/api/settings/classification/{}/dismiss"
    elsewhere = await client.post(url.format(B.id), json={"revision": 1})
    assert elsewhere.status_code == 409  # B was classified: nothing to act on
    assert (await client.post(url.format("2610.99999"), json={"revision": 1})).status_code == 404


async def test_dismissing_drops_the_paper_from_processing(client, conn, models):
    dismissed = await client.post(f"/api/settings/classification/{A.id}/dismiss", json=AT_1)
    assert dismissed.status_code == 204
    assert await awaiting(client) == []
    assert (await client.get("/api/status")).json()["state"] == "quiet"
    rewritten = A.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(conn, [rewritten])
    models.asked.clear()
    await night(conn, models)
    assert ("classify", A.title) not in models.asked  # not even after a rewrite


async def test_classifying_by_hand_goes_on_as_usual_with_source_owner(client, conn, models):
    bad = await client.put(
        f"/api/settings/classification/{A.id}", json=AT_1 | {"classes": ["nope"]}
    )
    assert bad.status_code == 422
    done = await client.put(
        f"/api/settings/classification/{A.id}",
        json=AT_1 | {"classes": ["interpretability", "agents"], "primary": "agents"},
    )
    assert done.json() == {"classes": ["interpretability", "agents"], "primary": "agents"}
    assert await members(conn, A.id) == {("interpretability", "owner"), ("agents", "owner")}
    (row,) = await rows(conn, "SELECT * FROM classifications WHERE paper_id = %s", A.id)
    assert (row["model"], row["primary_class"], row["response"]) == ("owner", "agents", None)
    assert await awaiting(client) == []
    (event,) = await rows(
        conn, "SELECT * FROM outbox WHERE type = 'paper.classified' AND paper_id = %s", A.id
    )
    assert event["payload"]["by_owner"] is True
    models.asked.clear()
    await night(conn, models)
    assert ("analyse", A.title) in models.asked  # and analysis follows
    assert ("classify", A.title) not in models.asked


async def test_fixing_the_text_counts_as_a_rewrite_and_classifies_again(client, conn, models):
    empty = await client.post(f"/api/settings/classification/{A.id}/correction", json=AT_1)
    assert empty.status_code == 422
    fixed = await client.post(
        f"/api/settings/classification/{A.id}/correction", json=AT_1 | {"title": "Probes, fixed"}
    )
    assert fixed.status_code == 204
    stored: Paper = await papers_store.get(conn, A.id)
    assert (stored.title, stored.revision) == ("Probes, fixed", 2)
    (event,) = await rows(
        conn, "SELECT * FROM outbox WHERE type = 'paper.revised' AND paper_id = %s", A.id
    )
    assert event["payload"] == {"rewritten": True, "corrected": True}
    assert await awaiting(client) == []
    await night(conn, models)
    assert ("other", "classifier") in await members(conn, A.id)


async def test_a_correction_that_cannot_settle_is_not_kept(client, conn, monkeypatch):
    """The new text and the grades without the old text's Weight are stored together, so a
    failed settle leaves the old text current and the correction can be made again."""

    async def broken(*_):
        raise RuntimeError("settle failed")

    monkeypatch.setattr(settings_api.word, "settle", broken)
    url = f"/api/settings/classification/{A.id}/correction"
    with pytest.raises(RuntimeError):
        await client.post(url, json=AT_1 | {"title": "Probes, fixed"})
    assert (await papers_store.get(conn, A.id)).revision == 1
    monkeypatch.undo()
    assert (await client.post(url, json=AT_1 | {"title": "Probes, fixed"})).status_code == 204


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/dismiss", {}),
        ("PUT", "", {"classes": ["agents"]}),
        ("POST", "/correction", {"title": "Probes, fixed"}),
    ],
)
async def test_an_action_on_text_that_changed_meanwhile_is_refused(
    client, conn, method, path, body
):
    url = f"/api/settings/classification/{A.id}{path}"
    # A revision the owner never saw.
    assert (await client.request(method, url, json=body | {"revision": 2})).status_code == 409
    # The nightly run rewrote the paper after Quality listed it at revision 1.
    rewritten = A.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(conn, [rewritten])
    refused = await client.request(method, url, json=body | AT_1)
    assert refused.status_code == 409
    # Nor does the new text wait for the owner: its classification never failed.
    assert (await client.request(method, url, json=body | {"revision": 2})).status_code == 409
    stored = await papers_store.get(conn, A.id)
    assert (stored.title, stored.abstract, stored.revision) == (A.title, rewritten.abstract, 2)
    assert await rows(conn, "SELECT 1 FROM papers WHERE dismissed_at IS NOT NULL") == []
    assert await rows(conn, "SELECT 1 FROM classifications WHERE paper_id = %s", A.id) == []
