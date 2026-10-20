"""The monitoring side: `/metrics` and `/status` read the run records, a run whose process died
reads as failed, and a nightly that did not happen reads as late. The status document says when
litwatch cannot do its job (data.md, the status document): many papers stopped at once, a
provider failed for its account or for the night; one stuck paper is the app's alone."""

from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from pydantic import BaseModel

from litwatch.api import create_app
from litwatch.api.monitor import Facts, Job, Night, components, create_monitor, due_night, metrics
from litwatch.config import Config, Models, RoleConfig, Schedule
from litwatch.core import FAILED_RUNS
from litwatch.llm import CallError, Gateway, ProviderError, Reply, Request, Stop, Usage
from litwatch.store import db, failures, reading, runs
from litwatch.store import fulltext as fulltext_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store.calls import CallJournal
from tests.conftest import FAITH, INTERP, TREE, paper
from tests.fakes import ScriptedProvider

STOPPED = {
    "awaiting_classification": 0,
    "analysis_failed": 0,
    "ungraded": 0,
    "fulltext_failed": 0,
    "digests_failed": 0,
}


@pytest.fixture
async def monitor(conn):
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_monitor(create_app(dsn, "t0ken"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m") as c:
        yield c


def parsed(text: str) -> dict[tuple[str, tuple], float]:
    """The exposition as Prometheus reads it; the parser raises on what it would reject."""
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }


def sample(text: str, name: str, job: str = "litwatch/nightly") -> float | None:
    return parsed(text).get((name, (("job_id", job),)))


def component(document: dict, id_: str) -> dict:
    return next(c for c in document["components"] if c["id"] == id_)


async def test_a_finished_nightly_is_measured_from_its_record(conn, monitor):
    run_id = await runs.start(conn, "nightly")
    await runs.finish(conn, run_id, counts={"classified": 42})

    text = (await monitor.get("/metrics")).text
    assert sample(text, "homelab_job_last_items") == 42
    # An empty queue reads 0, not no data.
    brief_ready = ("litwatch_fulltext_passes", (("kind", "brief"), ("state", "ready")))
    assert parsed(text)[brief_ready] == 0
    assert sample(text, "homelab_job_running") == 0
    assert sample(text, "homelab_job_last_success_timestamp_seconds") >= sample(
        text, "homelab_job_last_run_timestamp_seconds"
    )
    assert sample(text, "homelab_job_last_run_timestamp_seconds", "litwatch/publications") is None
    started = (await runs.latest(conn, "nightly"))["started_at"].timestamp()
    assert sample(text, "homelab_job_last_run_timestamp_seconds") == pytest.approx(
        started, abs=1e-3
    )
    status = (await monitor.get("/status")).json()
    assert status["schema"] == "homelab.status/v1"
    assert component(status, "nightly")["status"] == "healthy"


async def test_a_nightly_whose_process_died_reads_failed_not_running(conn, monitor):
    # A process that opens its run and is killed: its session ends, the record stays open.
    other = await db.sibling(conn)
    await runs.start(other, "nightly")
    text = (await monitor.get("/metrics")).text
    assert sample(text, "homelab_job_running") == 1
    assert component((await monitor.get("/status")).json(), "nightly")["status"] == "healthy"

    await other.close()
    text = (await monitor.get("/metrics")).text
    assert sample(text, "homelab_job_running") == 0
    assert sample(text, "homelab_job_last_run_timestamp_seconds") is not None
    assert sample(text, "homelab_job_last_success_timestamp_seconds") == 0
    nightly = component((await monitor.get("/status")).json(), "nightly")
    assert nightly["status"] == "failed"
    assert "ended without closing it" in nightly["reason"]


async def test_a_run_that_raised_reads_failed_with_its_error(conn, monitor):
    run_id = await runs.start(conn, "nightly")
    await runs.finish(conn, run_id, error="RuntimeError: boom")
    # Before the first success, last success is 0, so a first failure alerts as one.
    text = (await monitor.get("/metrics")).text
    assert sample(text, "homelab_job_last_success_timestamp_seconds") == 0
    nightly = component((await monitor.get("/status")).json(), "nightly")
    assert nightly["status"] == "failed"
    assert "boom" in nightly["reason"] and "expected" in nightly["reason"]


async def test_what_the_owner_acknowledged_still_reaches_the_status_document(conn, monitor):
    # Acknowledge on Needs you clears the app's run status only (DESIGN.md, Needs you).
    await taxonomy_store.save(conn, TREE)
    await conn.execute(
        "INSERT INTO digests (topic, period, day, state, acknowledged_at) "
        "VALUES (%s, 'day', '2026-10-18', 'failed', now())",
        (FAITH.slug,),
    )
    assert (await reading.status(conn))["digests_failed"] == 0
    assert parsed((await monitor.get("/metrics")).text)[("litwatch_digests_failed", ())] == 1
    assert component((await monitor.get("/status")).json(), "digests")["status"] == "degraded"


SCHEDULED = Config(schedule=Schedule(enabled=True, timezone="America/Denver"))


def facts(now: datetime, success_started: datetime | None, running: bool = False) -> Facts:
    ok = (
        {"started_at": success_started, "finished_at": success_started} if success_started else None
    )
    state = "running" if running else "quiet" if ok else "none"
    return Facts(
        now=now,
        jobs={"nightly": Job(ok, ok, running), "publications": Job(None, None, False)},
        status={"state": state, **STOPPED},
        passes={},
        cursors={},
        spend=[],
    )


def test_the_results_of_a_night_are_due_at_nine():
    # 02:00 nightly, 07:30 deadline, Denver (UTC-6 in October): due at 09:00, 15:00 UTC.
    fired, due = due_night(SCHEDULED, datetime(2026, 10, 20, 16, 0, tzinfo=UTC))
    assert (fired.astimezone(UTC), due.astimezone(UTC)) == (
        datetime(2026, 10, 20, 8, 0, tzinfo=UTC),
        datetime(2026, 10, 20, 15, 0, tzinfo=UTC),
    )
    # Before 09:00 the night before's is the one due.
    fired, _ = due_night(SCHEDULED, datetime(2026, 10, 20, 14, 0, tzinfo=UTC))
    assert fired.astimezone(UTC) == datetime(2026, 10, 19, 8, 0, tzinfo=UTC)
    assert due_night(Config(), datetime(2026, 10, 20, 16, 0, tzinfo=UTC)) is None


def test_a_missed_night_reads_degraded_and_a_done_one_healthy():
    now = datetime(2026, 10, 20, 16, 0, tzinfo=UTC)
    tonight = datetime(2026, 10, 20, 8, 0, 5, tzinfo=UTC)
    last_night = datetime(2026, 10, 19, 8, 0, 5, tzinfo=UTC)
    assert components(facts(now, tonight), SCHEDULED)[0]["status"] == "healthy"
    late = components(facts(now, last_night), SCHEDULED)[0]
    assert late["status"] == "degraded"
    assert "expected one by 2026-10-20T09:00:00-06:00" in late["reason"]
    # Still running past the time the owner reads: late all the same.
    running = components(facts(now, last_night, running=True), SCHEDULED)[0]
    assert running["status"] == "degraded"
    assert "still running" in running["reason"]
    # The scheduler off (the switch night): no night is expected.
    assert components(facts(now, last_night), Config())[0]["status"] == "healthy"


def test_every_unhealthy_component_says_why():
    f = facts(datetime(2026, 10, 20, 16, 0, tzinfo=UTC), None)
    f.status |= {"digests_failed": 1}
    f.night = Night(7, 100, {"analysis": 6}, {"openrouter": {"failed": 3, "replied": 0}})
    f.probes = {"archive": "ConnectError: refused", "deep": None}
    f.signin = {"state": "ok"}
    found = {c["id"]: c for c in components(f, SCHEDULED)}
    assert {i: c["status"] for i, c in found.items()} == {
        "nightly": "degraded",
        "pipeline": "degraded",
        "digests": "degraded",
        "provider-anthropic": "healthy",
        "provider-openrouter": "degraded",
        "archive": "failed",
        "deep": "healthy",
        "budget": "healthy",
    }
    assert all(c.get("reason") for c in found.values() if c["status"] != "healthy")
    assert "6 at analysis" in found["pipeline"]["reason"]
    assert "expected an answer within 3 s" in found["archive"]["reason"]
    assert parsed(metrics(f))[("litwatch_up", (("dependency", "archive"),))] == 0


def test_a_spring_forward_morning_finds_the_night_before():
    # 2027-03-14: 02:00 does not exist in Denver; at 08:00 local (14:00 UTC) the 13th's is due.
    fired, _ = due_night(SCHEDULED, datetime(2027, 3, 14, 14, 0, tzinfo=UTC))
    assert fired.astimezone(UTC) == datetime(2027, 3, 13, 9, 0, tzinfo=UTC)


def test_the_deadline_moves_due():
    late_deadline = Config(schedule=Schedule(enabled=True, timezone="UTC", deadline=time(9, 0)))
    _, due = due_night(late_deadline, datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    assert due == datetime(2026, 10, 20, 10, 30, tzinfo=UTC)


def test_label_values_are_escaped():
    f = facts(datetime(2026, 10, 20, 16, 0, tzinfo=UTC), None)
    f.passes = {("pdf", 'say "hi"\\n'): 1}
    labels = (("kind", "pdf"), ("state", 'say "hi"\\n'))
    assert parsed(metrics(f))[("litwatch_fulltext_passes", labels)] == 1


def test_more_papers_stopped_at_one_step_than_two_percent_of_the_night_turn_pipeline():
    f = facts(datetime(2026, 10, 20, 16, 0, tzinfo=UTC), None)

    def pipeline(stopped: dict[str, int], classified: int) -> dict:
        f.night = Night(7, classified, stopped, {})
        return next(c for c in components(f, Config()) if c["id"] == "pipeline")

    # 530 classified: up to 10.6 at one step is a bad night for those papers, not for litwatch.
    assert pipeline({"analysis": 10, "grading": 10, "fulltext": 10}, 530)["status"] == "healthy"
    many = pipeline({"analysis": 3, "fulltext": 11}, 530)
    assert many["status"] == "degraded"
    assert many["reason"] == (
        "papers stopped in nightly 7, which classified 530: 11 at the Brief and Weight passes; "
        "expected at most 10.6 at a step"
    )
    # A quiet night still allows five.
    assert pipeline({"classification": 5}, 20)["status"] == "healthy"
    assert pipeline({"classification": 6}, 20)["status"] == "degraded"


PAPERS = [paper(f"2610.{n:05d}") for n in range(1, 9)]


async def stop(conn, p, step: str = "grading", topic: str = FAITH.slug) -> None:
    """Unusable answers `FAILED_RUNS` runs in a row: the step waits for the owner."""
    for _ in range(FAILED_RUNS):
        await failures.failed(conn, p, step, "unusable", counts=True, topic=topic)


async def night(conn, classified: int = 100) -> int:
    """A nightly that started an hour ago and ends now."""
    row = await (
        await conn.execute(
            "INSERT INTO runs (kind, started_at, finished_at, counts) "
            "VALUES ('nightly', now() - interval '1 hour', now(), %s) RETURNING id",
            (f'{{"classified": {classified}}}',),
        )
    ).fetchone()
    return row["id"]


async def test_a_single_stuck_paper_leaves_pipeline_healthy(conn, monitor):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, PAPERS)
    await stop(conn, PAPERS[0])
    await night(conn)
    assert component((await monitor.get("/status")).json(), "pipeline")["status"] == "healthy"
    # It waits on Needs you, and the metrics still count it.
    grading = ("litwatch_stuck_papers", (("step", "grading"),))
    assert parsed((await monitor.get("/metrics")).text)[grading] == 1


async def test_many_papers_stopped_at_one_step_in_the_night_turn_pipeline_degraded(conn, monitor):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, PAPERS)
    for p in PAPERS[:5]:  # five papers, each on two topics: five
        await stop(conn, p)
        await stop(conn, p, topic=INTERP.slug)
    await stop(conn, PAPERS[5])  # stopped the night before
    await conn.execute(
        "UPDATE step_failures SET failed_at = now() - interval '1 day' WHERE paper_id = %s",
        (PAPERS[5].id,),
    )
    await failures.failed(conn, PAPERS[6], "grading", "unusable", counts=True, topic=FAITH.slug)
    run = await night(conn)  # one failed run is not stopped
    assert component((await monitor.get("/status")).json(), "pipeline")["status"] == "healthy"

    await stop(conn, PAPERS[7])
    await conn.execute("UPDATE runs SET finished_at = now() WHERE id = %s", (run,))
    pipeline = component((await monitor.get("/status")).json(), "pipeline")
    assert pipeline["status"] == "degraded"
    assert pipeline["reason"] == (
        f"papers stopped in nightly {run}, which classified 100: 6 at grading; "
        "expected at most 5 at a step"
    )


async def test_brief_and_weight_passes_count_when_they_stop_and_deep_passes_do_not(conn, monitor):
    await papers_store.upsert(conn, PAPERS)
    kinds = [(p, "deep") for p in PAPERS[:6]] + [
        (p, ("brief", "weight")[i % 2]) for i, p in enumerate(PAPERS[:6])
    ]
    for p, kind in kinds:
        await conn.execute(
            "INSERT INTO fulltext_passes (paper_id, paper_revision, kind, reason, state, "
            "priority, failed_runs) VALUES (%s, 1, %s, 'r', 'ready', 1, %s)",
            (p.id, kind, FAILED_RUNS - 1),
        )
    # Every try claimed before the night started, each failing a third time in it.
    text = await fulltext_store.claim(conn, timedelta(hours=1), limit=20)
    deep = await fulltext_store.claim(conn, timedelta(hours=1), limit=20, deep=True)
    await conn.execute("UPDATE fulltext_passes SET tried_at = now() - interval '1 day'")
    assert (len(text), len(deep)) == (6, 6)  # `deep` has the Deep passes' service failures
    for t in deep + text[:5]:
        await fulltext_store.failed(conn, t, "no text to be had", counts=True)
    run = await night(conn)
    assert component((await monitor.get("/status")).json(), "pipeline")["status"] == "healthy"

    await fulltext_store.failed(conn, text[5], "no text to be had", counts=True)
    await conn.execute("UPDATE runs SET finished_at = now() WHERE id = %s", (run,))
    pipeline = component((await monitor.get("/status")).json(), "pipeline")
    assert "6 at the Brief and Weight passes" in pipeline["reason"]


class Yes(BaseModel):
    yes: bool


def question(about: str) -> Request:
    return Request(system="s", user=about, output=Yes)


def answer(role: RoleConfig, request: Request) -> str | Reply | ProviderError:
    usage = Usage(model=role.model, via=role.provider, cost=0.001)
    match request.user:
        case "revoked":
            return ProviderError("AuthenticationError: invalid x-api-key", account=True)
        case "down":
            return ProviderError("InternalServerError: 503", transient=True)
        case "slow":
            return ProviderError("APITimeoutError", timeout=True)
        case "refused":
            return Reply("", Stop.REFUSED, usage, refusal="bio")
    return '{"yes": true}'


def gateway(conn, run: int) -> Gateway:
    models = Models(
        analyse=RoleConfig(provider="anthropic", model="a"),
        brief=RoleConfig(provider="openrouter", model="lab/b"),
    )
    providers = {n: ScriptedProvider(n, answer) for n in ("anthropic", "openrouter")}
    return Gateway(models, providers, CallJournal(conn, run), retry_delays=(0.0,))


async def test_an_account_error_fails_its_provider_until_its_next_reply(conn, monitor):
    gw = gateway(conn, await runs.start(conn, "nightly"))

    async def anthropic() -> dict:
        return component((await monitor.get("/status")).json(), "provider-anthropic")

    await gw.ask("analyse", question("fine"))
    with pytest.raises(CallError) as found:
        await gw.ask("analyse", question("revoked"))
    assert found.value.kind == "account"
    failed = await anthropic()
    assert failed["status"] == "degraded"
    assert failed["reason"].startswith("account: AuthenticationError: invalid x-api-key at ")
    assert failed["reason"].endswith(", no reply since; expected a reply after it")
    # An answer from the journal is not the provider's reply; the other provider's is not either.
    await gw.ask("analyse", question("fine"))
    await gw.ask("brief", question("fine"))
    assert (await anthropic())["status"] == "degraded"
    # Any reply is, a refusal included.
    with pytest.raises(CallError):
        await gw.ask("analyse", question("refused"))
    assert (await anthropic())["status"] == "healthy"


async def test_a_night_with_failed_calls_and_no_reply_from_a_provider_fails_it(conn, monitor):
    run = await runs.start(conn, "nightly")
    gw = gateway(conn, run)
    await gw.ask("brief", question("fine"))  # before the night: reused in it
    gw.journal = CallJournal(conn, run := await runs.start(conn, "nightly"))
    for about in ("down", "slow"):
        with pytest.raises(CallError):
            await gw.ask("brief", question(about))
    await gw.ask("brief", question("fine"))
    with pytest.raises(CallError):
        await gw.ask("analyse", question("down"))
    await gw.ask("analyse", question("fine"))

    def provider(document: dict, name: str) -> dict:
        return component(document, f"provider-{name}")

    # Still running: the night is not over.
    assert provider((await monitor.get("/status")).json(), "openrouter")["status"] == "healthy"
    await runs.finish(conn, run)
    document = (await monitor.get("/status")).json()
    assert provider(document, "anthropic")["status"] == "healthy"
    openrouter = provider(document, "openrouter")
    assert openrouter["status"] == "degraded"
    assert openrouter["reason"] == (
        f"2 calls in nightly {run} failed and none had a reply; expected a reply"
    )
    # A reply in the night that settled an older run's call (a dead run's batch) is the night's.
    (_,) = await (
        await conn.execute(
            "UPDATE inference_calls SET responded_at = r.finished_at "
            "FROM runs r WHERE r.id = %s AND inference_calls.run_id <> r.id "
            "AND usage->>'via' = 'openrouter' RETURNING inference_calls.id",
            (run,),
        )
    ).fetchall()
    assert provider((await monitor.get("/status")).json(), "openrouter")["status"] == "healthy"
