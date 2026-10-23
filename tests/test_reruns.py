"""Topic re-runs (docs/plans/jobs.md, P2): a re-run grades its scope for its topic again under
the topic's current version, each verdict settling its paper, and leaves every other paper
alone (V5); it never spends past its ceiling, sends no call that does not fit, stops as
`stopped` saying where, and Run now continues it (V4); it works the text passes its verdicts
queued under the same ceiling, never in the full-text worker, and none of its passes runs while
a nightly is running or due (V4b); the offer counts and estimates each scope. Models are
scripted, with known costs."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest

from litwatch import fulltext
from litwatch.api import create_app
from litwatch.archive import ArchiveClient
from litwatch.config import Config, Models, RoleConfig, Schedule
from litwatch.core import Cause, Fit, Label, LabelOrigin, LabelValue, Status, Verdict
from litwatch.core.fulltext import BACKGROUND_PRIORITY
from litwatch.feedback import word
from litwatch.fulltext import deep as deep_passes
from litwatch.jobs import reruns, worker
from litwatch.llm import CallError, Gateway, Reply, Request, Usage, prices
from litwatch.llm import ceiling as ceilings
from litwatch.llm.openrouter import OpenRouterProvider
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import runs
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts as verdicts_store
from tests.conftest import AGENTS, FAITH, TREE, paper
from tests.fakes import ScriptedProvider, SdkClient, openrouter_completion
from tests.fakes_http import FakeArchive, FakeDeep, mock
from tests.test_deep import DONE, make
from tests.test_fulltext import ANSWER, BODY
from tests.test_steps import ROLES, Scripted

BRIEF = RoleConfig(provider="anthropic", model="test-brief")
CONFIG = Config(models=Models(**ROLES, brief=BRIEF), schedule=Schedule(enabled=True))
ZONE = CONFIG.schedule.zone
AFTERNOON = datetime(2026, 10, 22, 14, 0, tzinfo=ZONE)  # the nightly (02:00) neither due nor run
NIGHT = datetime(2026, 10, 22, 2, 5, tzinfo=ZONE)  # five minutes after it was due
COST = 0.01  # what every call costs
TOKEN = "s3cret"

A = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes on activations.")
B = paper("2610.00002", title="Is the chain of thought faithful?", abstract="We test traces.")
C = paper("2610.00003", title="Agents that plan", abstract="Tool use over many steps.")


class Costed(ScriptedProvider):
    """A scripted provider whose every reply costs `COST`, keeping the limits each call was sent
    with (`max_price`)."""

    def __init__(self, name: str, respond) -> None:
        super().__init__(name, respond)
        self.limits: list[dict | None] = []

    async def complete(self, role: RoleConfig, request: Request, *, max_price=None) -> Reply:
        self.limits.append(max_price)
        reply = await super().complete(role, request)
        reply.usage.cost = COST
        return reply


class Priced:
    """The graders (`Scripted`) and `brief`, each call costing `COST`."""

    def __init__(self) -> None:
        self.scripted = Scripted()
        self.providers: dict[str, Costed] = {}

    def respond(self, role: RoleConfig, request) -> str | Reply:
        if role.model == BRIEF.model:
            return json.dumps(ANSWER)
        return self.scripted.respond(role, request)

    def gateway(self, config: Config, journal) -> Gateway:
        self.providers = {n: Costed(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, self.providers, journal, retry_delays=(0.0,))

    def sent(self, role: str) -> list[str]:
        """The titles `role` was asked about."""
        return [title for r, title in self.scripted.asked if r == role]


@pytest.fixture
async def models(conn, monkeypatch):
    """The taxonomy, three papers, prices for the test models on Anthropic, and history that
    bounds those on OpenRouter."""
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [A, B, C])
    for model in ("test-judge", "test-field", "test-brief", "test-analyse"):
        monkeypatch.setitem(prices.ANTHROPIC, model, (1.0, 2.0, 0.1))
    for model in ("test/check", "test/area-check"):
        await charged(conn, model, cost=0.0003, tokens_in=1000, tokens_out=1000)
    return Priced()


async def charged(
    conn, model: str, *, cost: float, tokens_in: int, tokens_out: int, cached: int = 0, by="X"
) -> None:
    """A billed OpenRouter call of `model` in the journal, on a run that is not a nightly's."""
    run_id = await runs.start(conn, "fulltext", None, None)
    usage = {
        "model": model,
        "via": "openrouter",
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "cached_tokens": cached,
        "cost": cost,
        "provider": by,
    }
    await conn.execute(
        "INSERT INTO inference_calls (run_id, operation, usage) VALUES (%s, 'check', %s)",
        (run_id, json.dumps(usage)),
    )
    await runs.finish(conn, run_id)


async def rerun(conn, topic: str, ids: list[str], ceiling: float, state: str = "queued") -> int:
    scope = {"topic": topic, "revision": 1, "scopes": ["members"], "papers": ids}
    return await jobs_store.create(conn, "rerun", scope, len(ids), state, ceiling=ceiling)


def archive(fake: FakeArchive) -> ArchiveClient:
    return ArchiveClient("http://archive.test", "archive-token", http=mock(fake.handle))


def with_text(*papers) -> FakeArchive:
    fake = FakeArchive()
    for p in papers:
        fake.texts[p.id] = (p.version, BODY)
    return fake


async def work_out(w: worker.Worker, now: datetime = AFTERNOON, most: int = 20) -> int:
    for n in range(most):
        if not await w.step(now):
            return n
    raise AssertionError("the worker never ran out of work")


async def rows(conn, query: str, *params) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def job_calls(conn, job_id: int) -> list[dict]:
    return await rows(
        conn,
        "SELECT c.* FROM inference_calls c JOIN runs r ON r.id = c.run_id WHERE r.job_id = %s "
        "ORDER BY c.id",
        job_id,
    )


async def graded_before(conn, p, topic: str, version: str, fit: str = "related") -> None:
    """A verdict recorded under an earlier version of the topic, settled."""
    v = Verdict(
        paper_id=p.id, topic=topic, topic_version=version, fit=Fit(fit), reason="r", opinions={}
    )
    await verdicts_store.add(conn, v)
    await word.settle(conn, p.id, cause=Cause.VERDICT)


async def edited(conn, topic, **change) -> str:
    """Save the topic edited; its new version."""
    tree = await taxonomy_store.load(conn)
    changed = tree.topic(topic.slug).model_copy(update=change)
    await word.save_taxonomy(conn, tree.following(changed))
    return changed.version


def most(request: Request) -> float:
    """The most a `test-field` call can cost, as the gateway reserves it."""
    tokens_in = ceilings.input_tokens(request)
    bound = ceilings.anthropic_bound(ROLES["field_grade"], tokens_in)
    return ceilings.Ceiling.most(bound, tokens_in, 16000)


# -- V5: the scope's papers, under the new version, each settled ----------------------------------


async def test_a_rerun_grades_its_scope_under_the_topics_new_version_and_no_other_paper(
    conn, models
):
    """A and B carry current verdicts under the old version, which `grading.due` would leave
    alone: the re-run of A alone grades A again under the new one, settles it, and touches
    neither B nor C."""
    old = FAITH.version
    for p in (A, B):
        await graded_before(conn, p, FAITH.slug, old)
    new = await edited(conn, FAITH, rubric="Central: probes that read hidden steps.")
    assert new != old
    models.scripted.fits[("judge", A.title)] = {"reason": "On the subject.", "fit": "central"}
    models.scripted.fits[("check", A.title)] = {"reason": "On the subject.", "fit": "central"}
    job = await rerun(conn, FAITH.slug, [A.id], ceiling=1.0)

    await work_out(worker.Worker(conn, CONFIG, models.gateway))

    found = await rows(conn, "SELECT paper_id, topic_version, fit FROM verdicts ORDER BY id")
    assert [(r["paper_id"], r["topic_version"]) for r in found] == [
        (A.id, old),
        (B.id, old),
        (A.id, new),
    ]
    assert found[-1]["fit"] == "central"
    # Settled: A's effective Fit follows its new verdict, with its event; B's is as it was.
    changed = await rows(
        conn, "SELECT paper_id, payload FROM outbox WHERE type = 'grade.changed' ORDER BY id"
    )
    assert changed[-1]["paper_id"] == A.id and changed[-1]["payload"]["fit_after"] == "central"
    assert {(r["paper_id"], r["payload"]["fit_after"]) for r in changed} == {
        (A.id, "related"),
        (B.id, "related"),
        (A.id, "central"),
    }
    assert set(models.sent("judge")) == {A.title} and set(models.sent("check")) == {A.title}
    done = await jobs_store.get(conn, job)
    assert (done["state"], done["done"], done["counts"]["graded"]) == ("done", 1, 1)


async def test_a_paper_graded_under_the_current_version_is_not_graded_again(conn, models):
    """Taken back, or continued, a re-run grades only what has no verdict under the version."""
    await graded_before(conn, A, AGENTS.slug, AGENTS.version)
    job = await rerun(conn, AGENTS.slug, [A.id, B.id], ceiling=1.0)
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    assert models.sent("field_grade") == [B.title]
    assert (await jobs_store.get(conn, job))["state"] == "done"


async def test_a_rerun_whose_topic_was_paused_fails_and_says_why(conn, models):
    job = await rerun(conn, AGENTS.slug, [A.id], ceiling=1.0)
    await edited(conn, AGENTS, status=Status.PAUSED)
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    stopped = await jobs_store.get(conn, job)
    assert stopped["state"] == "failed" and "no longer followed and active" in stopped["error"]
    assert models.sent("field_grade") == []


async def test_a_grading_that_fails_fails_the_rerun_and_try_again_does_what_is_left(conn, models):
    models.scripted.fits[("field_grade", B.title)] = {"fit": "nowhere"}
    job = await rerun(conn, AGENTS.slug, [A.id, B.id], ceiling=1.0)
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    failed = await jobs_store.get(conn, job)
    assert (failed["state"], failed["error"]) == ("failed", "grading failed on 1")
    # Recorded on the job, not as the paper's failure: no nightly takes it up.
    assert await rows(conn, "SELECT * FROM step_failures") == []

    del models.scripted.fits[("field_grade", B.title)]
    await jobs_store.move(conn, job, ("failed",), "queued")
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    assert (await jobs_store.get(conn, job))["state"] == "done"
    assert models.sent("field_grade").count(A.title) == 1  # graded once, not again


# -- V4: the ceiling -----------------------------------------------------------------------------


async def test_the_ceiling_is_never_passed_and_a_call_that_does_not_fit_is_not_sent(conn, models):
    """Each call may cost at most `most` and costs `COST`: under `most + 1.5 * COST`, two calls
    fit (the second once the first has settled), the third does not, with nothing in flight to
    free room. The job stops, says where, and Run now continues it under a new ceiling."""
    from litwatch import grading  # noqa: PLC0415

    name = TREE.get(AGENTS.slug).name
    worst = max(most(grading.request(p, AGENTS, name)) for p in (A, B, C))
    limit = worst + 1.5 * COST
    job = await rerun(conn, AGENTS.slug, [A.id, B.id, C.id], ceiling=limit)

    await work_out(worker.Worker(conn, CONFIG, models.gateway))

    stopped = await jobs_store.get(conn, job)
    assert stopped["state"] == "stopped"
    assert stopped["error"] == "stopped at the ceiling after 2 of 3"
    assert len(models.sent("field_grade")) == 2  # the third was never sent
    calls = await job_calls(conn, job)
    spent = sum(c["usage"]["cost"] or 0 for c in calls)
    assert spent == pytest.approx(2 * COST) and spent <= limit
    assert [c["error"].split(":")[0] for c in calls if c["error"]] == ["ceiling"]
    assert await jobs_store.spent(conn, job) == pytest.approx(2 * COST)

    async with api(conn) as http:
        # Not above what it spent: refused, and it stays where it is.
        low = await http.post(f"/api/jobs/{job}/run", json={"ceiling": 0.01})
        assert low.status_code == 409 and "above what it spent" in low.json()["detail"]
        assert (await http.post(f"/api/jobs/{job}/run", json={"ceiling": 1.0})).status_code == 200
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    done = await jobs_store.get(conn, job)
    assert (done["state"], done["error"], float(done["ceiling"])) == ("done", None, 1.0)
    assert sorted(models.sent("field_grade")) == sorted([A.title, B.title, C.title])
    assert await jobs_store.spent(conn, job) == pytest.approx(3 * COST)


async def test_a_call_waits_for_room_and_the_ceiling_counts_retries():
    """Reservations against one ceiling, as concurrent calls make them: a call that does not fit
    waits while another is in flight, and fits once its charge is known; with none in flight it
    stops everything after it. An unknown charge keeps its reservation."""
    import asyncio  # noqa: PLC0415

    c = ceilings.Ceiling(1.0, {})
    await c.reserve(0.6)
    waiting = asyncio.create_task(c.reserve(0.6))
    await asyncio.sleep(0)
    assert not waiting.done()
    await c.settle(0.6, 0.1)  # charged far less than reserved: room for the next
    await waiting
    assert c.left == pytest.approx(0.3)
    await c.settle(0.6, None)  # a timeout: it may have been billed in full
    assert (c.spent, c.left) == (pytest.approx(0.7), pytest.approx(0.3))
    with pytest.raises(ceilings.CeilingReachedError):
        await c.reserve(0.4)
    with pytest.raises(ceilings.CeilingReachedError):  # stopped: nothing more is sent
        await c.reserve(0.01)


async def test_an_openrouter_call_under_a_ceiling_carries_its_max_price_and_a_cap():
    sdk = SdkClient(openrouter_completion(text=json.dumps({"reason": "r", "fit": "related"})))
    role = RoleConfig(provider="openrouter", model="meta/muse")
    gateway = Gateway(Models(check=role), {"openrouter": OpenRouterProvider(client=sdk)})
    bound = ceilings.Bound(1e-7, 2e-7, 16000, {"prompt": 0.1, "completion": 0.2})
    gateway.ceiling = ceilings.Ceiling(1.0, {"openrouter:meta/muse": bound})
    from litwatch.grading import Output  # noqa: PLC0415

    await gateway.ask("check", Request(system="s", user="u", output=Output))
    (sent,) = sdk.sent
    assert sent["extra_body"]["provider"]["max_price"] == {"prompt": 0.1, "completion": 0.2}
    assert sent["max_completion_tokens"] == 16000  # the role sets no cap: the bound's
    assert gateway.ceiling.spent == pytest.approx(0.0021)  # the reply's charge

    # A route with no bound is not sent under a ceiling.
    gateway.ceiling = ceilings.Ceiling(1.0, {})
    with pytest.raises(CallError) as refused:
        await gateway.ask("check", Request(system="s", user="v", output=Output))
    assert refused.value.kind == "unpriced" and len(sdk.sent) == 1


async def test_an_openrouter_route_is_bound_by_what_its_calls_were_charged(conn):
    """Per serving provider, a call's charge over its tokens bounds each price, and the tightest
    holds; across providers, the highest. P charges $0.2/M in and $0.1/M out, Q $0.5/M and
    $0.05/M: the bound is Q's input and P's output, above each. A call with cached input is
    left out of the input price, which caching discounted. A Decisions route is held to its
    input."""
    await charged(conn, "m/x", cost=0.0003, tokens_in=1000, tokens_out=1000, by="P")
    await charged(conn, "m/x", cost=0.00022, tokens_in=100, tokens_out=2000, by="P")
    await charged(conn, "m/x", cost=0.0000718, tokens_in=1000, tokens_out=500, cached=900, by="P")
    await charged(conn, "m/x", cost=0.00055, tokens_in=1000, tokens_out=1000, by="Q")
    await charged(conn, "m/x", cost=0.00025, tokens_in=100, tokens_out=4000, by="Q")
    chat = await reruns.bound(conn, RoleConfig(provider="openrouter", model="m/x"))
    assert chat.max_price == pytest.approx({"prompt": 0.55, "completion": 0.11})
    assert (chat.input, chat.output) == (pytest.approx(0.55e-6), pytest.approx(0.11e-6))
    assert chat.max_output == 16000
    decide = RoleConfig(provider="openrouter", model="m/x", decisions=True)
    found = await reruns.bound(conn, decide)
    assert found.max_price == pytest.approx({"prompt": 0.55, "completion": 0})
    assert found.output == 0
    assert await reruns.bound(conn, RoleConfig(provider="openrouter", model="m/none")) is None


# -- V4b: its passes, in the job, under the ceiling, never while a nightly waits -----------------


async def focus_rerun(conn, models, ceiling: float) -> int:
    """A re-run of the Focus topic over A, which grades it central: its Brief pass and its Deep
    card follow."""
    for role in ("judge", "check"):
        models.scripted.fits[(role, A.title)] = {"reason": "On the subject.", "fit": "central"}
    return await rerun(conn, FAITH.slug, [A.id], ceiling=ceiling)


async def text_pass(conn) -> dict:
    (found,) = await rows(conn, "SELECT * FROM fulltext_passes WHERE kind <> 'deep'")
    return found


async def test_a_reruns_text_pass_runs_in_the_job_under_its_ceiling_and_nowhere_else(conn, models):
    job = await focus_rerun(conn, models, ceiling=1.0)
    w = worker.Worker(conn, CONFIG, models.gateway, archive=archive(with_text(A)))
    assert await w.step(AFTERNOON)  # the grading
    queued = await text_pass(conn)
    assert (queued["job_id"], queued["priority"]) == (job, BACKGROUND_PRIORITY)
    assert (await jobs_store.get(conn, job))["state"] == "running"

    # The full-text worker, and a nightly's, leave it to the job.
    assert await fulltext.work(conn, CONFIG, archive(with_text(A)), gateway=models.gateway) == {}
    assert (await text_pass(conn))["state"] == "awaiting_text"

    await work_out(w)
    assert (await text_pass(conn))["state"] == "done"
    done = await jobs_store.get(conn, job)
    assert (done["state"], done["counts"]["briefs_written"]) == ("done", 1)
    roles = [c["operation"] for c in await job_calls(conn, job)]
    assert sorted(roles) == ["brief", "check", "judge"]  # all on the job's run, its ceiling's
    (deep,) = await rows(conn, "SELECT * FROM fulltext_passes WHERE kind = 'deep'")
    assert (deep["job_id"], deep["priority"]) == (None, BACKGROUND_PRIORITY)


async def test_a_text_pass_that_does_not_fit_stops_the_rerun_and_waits_for_run_now(conn, models):
    """Two grading calls fit; the Brief's reservation does not: the pass goes back unworked and
    stays the job's, which stops saying what is left."""
    from litwatch import grading  # noqa: PLC0415

    request = grading.request(A, FAITH, TREE.get(FAITH.slug).name)
    tokens_in = ceilings.input_tokens(request)
    judge = ceilings.Ceiling.most(
        ceilings.anthropic_bound(ROLES["judge"], tokens_in), tokens_in, 16000
    )
    job = await focus_rerun(conn, models, ceiling=judge + 0.01)
    w = worker.Worker(conn, CONFIG, models.gateway, archive=archive(with_text(A)))
    await work_out(w)
    stopped = await jobs_store.get(conn, job)
    assert stopped["state"] == "stopped"
    assert stopped["error"] == "stopped at the ceiling with 1 full-text passes left"
    pass_ = await text_pass(conn)
    assert (pass_["state"], pass_["job_id"], pass_["failed_runs"]) == ("ready", job, 0)
    assert models.sent("judge") == [A.title]

    await jobs_store.move(conn, job, jobs_store.NOT_RUN, "queued", 1.0)
    await work_out(w)
    assert (await jobs_store.get(conn, job))["state"] == "done"
    assert (await text_pass(conn))["state"] == "done"


async def test_no_pass_of_a_rerun_runs_while_a_nightly_is_due_or_running(conn, models):
    job = await focus_rerun(conn, models, ceiling=1.0)
    w = worker.Worker(conn, CONFIG, models.gateway, archive=archive(with_text(A)))
    assert await w.step(AFTERNOON)  # graded; its passes queued
    assert not await w.step(NIGHT)  # a nightly is due: no chunk, so no pass
    assert (await text_pass(conn))["state"] == "awaiting_text"

    # The Deep worker holds the re-run's card while a nightly runs, and makes it after.
    service = FakeDeep(DONE)
    nightly = await runs.start(conn, "nightly", None, None)
    assert await deep_passes.work(conn, archive(with_text(A)), make(service), config=CONFIG) == {}
    assert service.requests == []
    await runs.finish(conn, nightly)
    done = await deep_passes.work(conn, archive(with_text(A)), make(service), config=CONFIG)
    assert done == {"cards_written": 1} and len(service.requests) == 1
    assert (await jobs_store.get(conn, job))["state"] == "running"  # its text pass still waits


async def test_a_discarded_rerun_lets_its_passes_go(conn, models):
    job = await focus_rerun(conn, models, ceiling=1.0)
    w = worker.Worker(conn, CONFIG, models.gateway)  # no archive: the pass waits
    assert await w.step(AFTERNOON)
    await jobs_store.finish(conn, job, "stopped", "stopped at the ceiling")  # as if stopped there
    async with api(conn) as http:
        assert (await http.post(f"/api/jobs/{job}/discard")).status_code == 200
    assert (await text_pass(conn))["job_id"] is None


# -- the offer -----------------------------------------------------------------------------------


@asynccontextmanager
async def api(conn, config=CONFIG):
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, config)
    transport = httpx.ASGITransport(app=app)
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        yield c


async def test_the_offer_counts_and_estimates_each_scope_and_starts_or_keeps_a_rerun(conn, models):
    """Labelled or missed, members, keyword matches over 90 days, a date range; the chosen ones
    together count a paper once. Without calls to estimate by, no estimate, and it says so."""
    from datetime import timedelta  # noqa: PLC0415

    now = datetime.now(UTC)
    for p, days in ((A, 1), (B, 10), (C, 200)):
        await papers_store.upsert(
            conn, [p.model_copy(update={"published": (now - timedelta(days)).isoformat()})]
        )
    label = Label(
        paper_id=C.id, topic=AGENTS.slug, value=LabelValue.RELATED, origin=LabelOrigin.READER
    )
    await word.label(conn, label)
    for p in (A, C):
        await conn.execute(
            "INSERT INTO memberships (paper_id, class, source, probability, class_version, "
            "paper_revision) VALUES (%s, %s, 'classifier', 0.9, 'v', 1)",
            (p.id, AGENTS.slug),
        )
    await edited(conn, AGENTS, keywords=["tool use", "probes"])
    query = {"topic": AGENTS.slug, "scopes": ["labels", "members", "range"]}
    since = (now - timedelta(days=12)).date().isoformat()
    async with api(conn) as http:
        offer = (await http.get("/api/jobs/reruns/offer", params=query)).json()
        dated = query | {"since": since, "until": now.date().isoformat()}
        offer_dated = (await http.get("/api/jobs/reruns/offer", params=dated)).json()

        rows_ = {r["scope"]: r["papers"] for r in offer["rows"]}
        assert rows_ == {"labels": 1, "members": 2, "keywords": 1, "range": 0}
        assert offer["papers"] == 2  # C is labelled and a member: once
        assert offer["estimate"] is None and offer["missing"] == ["field_grade"]
        assert offer["ceiling"] == 5.0 and offer["unpriced"] == []
        assert offer_dated["papers"] == 3  # A and B in the range, C labelled

        # Not now: kept under Not run; Run: queued.
        body = {"topic": AGENTS.slug, "scopes": ["members"], "ceiling": 2.5, "run": False}
        kept = (await http.post("/api/jobs/reruns", json=body)).json()
        (row,) = kept["not_run"]
        assert (row["kind"], row["state"], row["papers"], row["ceiling"]) == (
            "rerun",
            "offered",
            2,
            2.5,
        )
        assert row["topic"]["slug"] == AGENTS.slug and row["scopes"] == ["members"]
        started = (await http.post("/api/jobs/reruns", json=body | {"run": True})).json()
        # The same re-run asked again (a lost reply): the one kept is put in the queue, once.
        assert started["not_run"] == [] and [r["state"] for r in started["running"]] == ["queued"]
        again = (await http.post("/api/jobs/reruns", json=body | {"run": True})).json()
        assert [r["key"] for r in again["running"]] == [r["key"] for r in started["running"]]
        wider = body | {"scopes": ["labels", "members"], "run": True}
        assert len((await http.post("/api/jobs/reruns", json=wider)).json()["running"]) == 2
        empty = body | {"scopes": ["range"]}
        assert (await http.post("/api/jobs/reruns", json=empty)).status_code == 409
    async with api(conn, CONFIG.model_copy(update={"schedule": Schedule()})) as http:
        off = await http.post("/api/jobs/reruns", json=body | {"run": True})
        assert off.status_code == 409 and "schedule is off" in off.json()["detail"]


async def test_the_offer_estimates_from_the_roles_calls_and_names_unpriced_models(conn, models):
    run_id = await runs.start(conn, "fulltext", None, None)
    usage = Usage(model="test-field", via="anthropic", cost=0.002)
    await conn.execute(
        "INSERT INTO inference_calls (run_id, operation, usage) VALUES (%s, 'field_grade', %s)",
        (run_id, usage.model_dump_json()),
    )
    await papers_store.upsert(conn, [])
    for p in (A, B):
        await conn.execute(
            "INSERT INTO memberships (paper_id, class, source, probability, class_version, "
            "paper_revision) VALUES (%s, %s, 'classifier', 0.9, 'v', 1)",
            (p.id, AGENTS.slug),
        )
    del prices.ANTHROPIC["test-brief"]
    async with api(conn) as http:
        params = {"topic": AGENTS.slug, "scopes": ["members"]}
        offer = (await http.get("/api/jobs/reruns/offer", params=params)).json()
    assert offer["estimate"] == pytest.approx(0.004) and offer["missing"] == []
    assert offer["unpriced"] == ["brief: test-brief"]
