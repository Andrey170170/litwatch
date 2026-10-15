"""Classification and analysis as nightly steps: what they record, their events, failures and
the owner's actions on a paper classification keeps failing on. Models are scripted."""

import json

import httpx
import pytest

from litwatch import classification
from litwatch.api import create_app
from litwatch.config import Classification, Config, Models, RoleConfig
from litwatch.core import FAILED_RUNS, Paper
from litwatch.jobs import nightly
from litwatch.llm import Gateway
from litwatch.store import classifications as classifications_store
from litwatch.store import failures, runs
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import TREE, paper
from tests.fakes import ScriptedProvider

CLASSIFY = RoleConfig(provider="openrouter", model="test/decider")
CONFIG = Config(models=Models(classify=CLASSIFY))
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
    """Scripted `classify` and `analyse` roles, answering by the paper's title."""

    def __init__(self) -> None:
        self.decisions: dict[str, dict] = {}
        self.analyses: dict[str, dict] = {}
        self.asked: list[tuple[str, str]] = []  # (role, title)
        self.users: list[str] = []  # every request's user message

    def respond(self, role: RoleConfig, request) -> str:
        title = request.user.split("Title: ", 1)[1].split("\n", 1)[0]
        step = "classify" if role.model == CLASSIFY.model else "analyse"
        self.asked.append((step, title))
        self.users.append(request.user)
        if step == "classify":
            return json.dumps(self.decisions.get(title, decision()))
        return json.dumps(self.analyses.get(title, ANALYSIS))

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


async def test_failed_analysis_is_retried_every_run_and_listed_after_three(conn, models):
    models.analyses[A.title] = ANALYSIS | {"gist": "Too short."}  # not a Gist: invalid
    for _ in range(FAILED_RUNS):
        assert (await night(conn, models))["analysis_failed"] == 1
    (listed,) = await failures.stuck(conn, "analysis", FAILED_RUNS)
    assert listed["paper_id"] == A.id and listed["title"] == A.title
    del models.analyses[A.title]
    assert (await night(conn, models))["analysed"] == 1  # still tried, and now it worked
    assert await failures.stuck(conn, "analysis", FAILED_RUNS) == []


async def test_a_result_for_text_that_changed_meanwhile_records_nothing(conn, models):
    tree = await taxonomy_store.load(conn)
    gateway = models.gateway(CONFIG, None)
    (found,) = [c async for c in classification.classify([A], tree, gateway)]
    rewritten = A.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(conn, [rewritten])
    assert not await classification.record(conn, found)
    assert await rows(conn, "SELECT 1 FROM classifications") == []
    assert await rows(conn, "SELECT 1 FROM outbox WHERE type = 'paper.classified'") == []


# -- the owner's three actions on a paper awaiting classification --------------------------------

TOKEN = "s3cret"


@pytest.fixture
async def client(conn, models):
    models.decisions[A.title] = {"broken": True}  # never valid
    for _ in range(FAILED_RUNS):
        await night(conn, models)
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, CONFIG)
    transport = httpx.ASGITransport(app=app)
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
    elsewhere = await client.post(f"/api/settings/classification/{B.id}/dismiss")
    assert elsewhere.status_code == 404  # B was classified: nothing to act on


async def test_dismissing_drops_the_paper_from_processing(client, conn, models):
    assert (await client.post(f"/api/settings/classification/{A.id}/dismiss")).status_code == 204
    assert await awaiting(client) == []
    assert (await client.get("/api/status")).json()["state"] == "quiet"
    rewritten = A.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(conn, [rewritten])
    models.asked.clear()
    await night(conn, models)
    assert ("classify", A.title) not in models.asked  # not even after a rewrite


async def test_classifying_by_hand_goes_on_as_usual_with_source_owner(client, conn, models):
    bad = await client.put(f"/api/settings/classification/{A.id}", json={"classes": ["nope"]})
    assert bad.status_code == 422
    done = await client.put(
        f"/api/settings/classification/{A.id}",
        json={"classes": ["interpretability", "agents"], "primary": "agents"},
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
    empty = await client.post(f"/api/settings/classification/{A.id}/correction", json={})
    assert empty.status_code == 422
    fixed = await client.post(
        f"/api/settings/classification/{A.id}/correction", json={"title": "Probes, fixed"}
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
