"""The owner's queue (docs/plans/jobs.md, P1): the worker runs a job's papers, and only those,
through classification, analysis and grading, a chunk at a time under the run lock; two workers
never run one job, and a job whose session died is taken back; no chunk starts while a nightly
run is due or running; `/api/jobs` lists and acts on jobs, and Process queues one. Models are
scripted (`test_steps.Scripted`)."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
import pytest

from litwatch import store
from litwatch.api import create_app
from litwatch.api import reading as api_reading
from litwatch.config import Schedule
from litwatch.core import Cause
from litwatch.core.fulltext import REQUEST_PRIORITY, TEXT_REQUEST_PRIORITY
from litwatch.feedback import misses, word
from litwatch.jobs import nightly, worker
from litwatch.jobs.context import model_run
from litwatch.jobs.lock import run_lock
from litwatch.store import failures, runs
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import TREE, answering, paper
from tests.test_steps import CONFIG, Scripted, decision

SCHEDULED = CONFIG.model_copy(update={"schedule": Schedule(enabled=True)})  # the worker runs

A = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes on activations.")
B = paper("2610.00002", title="Is the chain of thought faithful?", abstract="We test traces.")
C = paper("2610.00003", title="Agents that plan", abstract="Tool use over many steps.")

ZONE = CONFIG.schedule.zone
AFTERNOON = datetime(2026, 10, 22, 14, 0, tzinfo=ZONE)  # the nightly (02:00) neither due nor run
NIGHT = datetime(2026, 10, 22, 2, 5, tzinfo=ZONE)  # five minutes after it was due

TOKEN = "s3cret"


@pytest.fixture
async def models(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [A, B, C])
    return Scripted()


async def queued(conn, *ids: str, kind: str = "process") -> int:
    return await jobs_store.create(conn, kind, {"papers": list(ids)}, len(ids), "queued")


async def classified(conn) -> set[str]:
    rows = await (await conn.execute("SELECT DISTINCT paper_id FROM classifications")).fetchall()
    return {r["paper_id"] for r in rows}


async def rows(conn, query: str, *params) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def work_out(w: worker.Worker, now: datetime = AFTERNOON, most: int = 20) -> int:
    """Step the worker until it has nothing to run; the chunks it worked."""
    for n in range(most):
        if not await w.step(now):
            return n
    raise AssertionError("the worker never ran out of work")


def asked(models: Scripted, role: str, title: str) -> int:
    return models.asked.count((role, title))


# -- V1: a job runs its papers through the steps, and only those ---------------------------------


async def test_a_process_job_runs_its_paper_through_every_step_and_no_other(conn, models):
    job = await queued(conn, A.id)
    w = worker.Worker(conn, CONFIG, models.gateway)
    assert await work_out(w) == 1

    assert await classified(conn) == {A.id}  # B and C are due a nightly, and wait for it
    analysed = await rows(conn, "SELECT paper_id FROM analyses")
    assert [r["paper_id"] for r in analysed] == [A.id]
    passes = await rows(conn, "SELECT paper_id FROM grading_passes")
    assert [r["paper_id"] for r in passes] == [A.id]
    assert {t for t, title in models.asked if title != A.title} == set()
    done = await jobs_store.get(conn, job)
    assert (done["state"], done["done"], done["error"]) == ("done", 1, None)
    assert done["counts"]["classified"] == 1 and done["counts"]["analysed"] == 1
    # One run of kind `job`, closed, with the job's cost; the Nights table reads nightly runs only.
    (run,) = await rows(conn, "SELECT * FROM runs WHERE job_id = %s", job)
    assert (run["kind"], run["error"]) == ("job", None) and run["finished_at"] is not None
    assert run["cost"] > 0 and run["counts"]["classified"] == 1
    await nightly.run(conn, CONFIG, fetch=False, gateway=models.gateway, with_digests=False)
    async with api(conn) as http:
        nights = (await http.get("/api/settings/runs")).json()
    assert [r["kind"] for r in nights] == ["nightly"]


async def test_a_job_of_many_chunks_is_one_run(conn, models, monkeypatch):
    monkeypatch.setattr(worker, "CHUNK", 1)
    job = await queued(conn, A.id, B.id)
    w = worker.Worker(conn, CONFIG, models.gateway)
    assert await work_out(w) == 2
    assert await classified(conn) == {A.id, B.id}
    (run,) = await rows(conn, "SELECT * FROM runs WHERE job_id = %s", job)
    assert run["counts"]["classified"] == 2 and run["finished_at"] is not None
    assert (await jobs_store.get(conn, job))["state"] == "done"


async def test_a_job_whose_paper_failed_a_step_fails_and_says_where(conn, models):
    models.decisions[A.title] = {"broken": True}
    job = await queued(conn, A.id)
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    failed = await jobs_store.get(conn, job)
    assert (failed["state"], failed["error"]) == ("failed", "classification failed on 1")

    # Try again starts it over: what is still due runs, nothing else.
    del models.decisions[A.title]
    assert (await jobs_store.move(conn, job, ("failed",), "queued"))["error"] is None
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    again = await jobs_store.get(conn, job)
    assert (again["state"], again["counts"]["classified"]) == ("done", 1)


async def test_a_job_tried_again_on_a_step_waiting_for_the_owner_fails_again(conn, models):
    """Three runs in a row failed, the step waits for the owner on Needs you: Try again has
    nothing due and fails again, saying so, until the owner acts there; then the job goes back
    in the queue by itself and does what is left."""
    models.decisions[A.title] = {"broken": True}
    job = await queued(conn, A.id)
    for _ in range(3):
        await work_out(worker.Worker(conn, CONFIG, models.gateway))
        await jobs_store.move(conn, job, ("failed",), "queued")
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    stuck = await jobs_store.get(conn, job)
    assert stuck["state"] == "failed"
    assert stuck["error"] == "classification failed on 1 (waits on Needs you)"
    assert asked(models, "classify", A.title) == 3

    del models.decisions[A.title]
    async with word.change(conn, A.id, cause=Cause.OWNER):  # as Needs you's own retry does
        await failures.succeeded(conn, A.id, "classification")
    assert (await jobs_store.get(conn, job))["state"] == "queued"
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    assert (await jobs_store.get(conn, job))["state"] == "done"
    assert asked(models, "classify", A.title) == 4


async def test_a_failure_the_owner_clears_while_the_job_ends_still_settles_it(
    conn, models, monkeypatch
):
    models.decisions[A.title] = {"broken": True}
    job = await queued(conn, A.id)
    read, owner = failures.open_on, None
    async with await store.sibling(conn) as other:

        async def clear():  # Needs you's retry, from another request
            async with word.change(other, A.id, cause=Cause.OWNER):
                await failures.succeeded(other, A.id, "classification")

        async def raced(*args):
            nonlocal owner
            found = await read(*args)
            owner = asyncio.create_task(clear())
            await asyncio.sleep(0.3)
            return found

        monkeypatch.setattr(failures, "open_on", raced)
        assert await worker.Worker(conn, CONFIG, models.gateway).step(AFTERNOON)
        await owner
    assert (await jobs_store.get(conn, job))["state"] == "queued"  # failed, then settled


async def test_a_failed_job_whose_paper_the_owner_dismisses_is_discarded(conn, models):
    models.decisions[A.title] = {"broken": True}
    job = await queued(conn, A.id)
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    async with word.change(conn, A.id, cause=Cause.OWNER):
        await papers_store.dismiss(conn, A.id)
    assert (await jobs_store.get(conn, job))["state"] == "discarded"


async def test_a_process_jobs_passes_go_as_the_owners_requests(conn, models):
    models.decisions[A.title] = decision("cot-faithfulness", **{"cot-faithfulness": 0.9})
    models.fits = {(r, A.title): {"reason": "Core.", "fit": "central"} for r in ("judge", "check")}
    await queued(conn, A.id)
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    passes = await rows(conn, "SELECT * FROM fulltext_passes WHERE paper_id = %s", A.id)
    assert {p["kind"] for p in passes} == {"brief", "deep"}
    for p in passes:
        first = REQUEST_PRIORITY if p["kind"] == "deep" else TEXT_REQUEST_PRIORITY
        assert p["priority"] == first, p


async def test_a_process_jobs_paper_the_nightly_took_first_still_goes_as_the_owners(conn, models):
    """Process while the nightly is due: the night processes the paper, its passes the owner's."""
    models.decisions[A.title] = decision("cot-faithfulness", **{"cot-faithfulness": 0.9})
    models.fits = {(r, A.title): {"reason": "Core.", "fit": "central"} for r in ("judge", "check")}
    await queued(conn, A.id)
    await nightly.run(conn, CONFIG, fetch=False, gateway=models.gateway, with_digests=False)
    passes = await rows(conn, "SELECT * FROM fulltext_passes WHERE paper_id = %s", A.id)
    assert {(p["kind"], p["priority"]) for p in passes} == {
        ("brief", TEXT_REQUEST_PRIORITY),
        ("deep", REQUEST_PRIORITY),
    }


async def test_a_job_without_a_taxonomy_fails_and_says_so(conn):
    await papers_store.upsert(conn, [A])
    job = await queued(conn, A.id)
    assert await worker.Worker(conn, CONFIG, Scripted().gateway).step(AFTERNOON)
    failed = await jobs_store.get(conn, job)
    assert (failed["state"], failed["error"]) == ("failed", "no taxonomy yet")


async def test_a_job_that_raises_fails_with_its_error_and_its_run_records_it(
    conn, models, monkeypatch
):
    async def broken(*_, **__):
        raise RuntimeError("the store went away")

    monkeypatch.setattr(nightly, "analyse", broken)
    job = await queued(conn, A.id)
    assert await worker.Worker(conn, CONFIG, models.gateway).step(AFTERNOON)
    failed = await jobs_store.get(conn, job)
    assert (failed["state"], failed["error"]) == ("failed", "RuntimeError: the store went away")
    (run,) = await rows(conn, "SELECT * FROM runs WHERE job_id = %s", job)
    assert run["error"] == "RuntimeError: the store went away" and run["finished_at"]


# -- V2: one job, one worker; a dead job is taken back --------------------------------------------


async def test_two_workers_never_run_one_job(conn, models):
    job = await queued(conn, A.id)
    async with await store.sibling(conn) as other:
        first = worker.Worker(conn, CONFIG, models.gateway)
        second = worker.Worker(other, CONFIG, models.gateway)
        ran = await asyncio.gather(first.step(AFTERNOON), second.step(AFTERNOON))
        await second.close(RuntimeError("test over"))
    assert sorted(ran) == [False, True]
    assert asked(models, "classify", A.title) == 1
    assert (await jobs_store.get(conn, job))["state"] == "done"


async def test_a_claim_in_progress_is_skipped_and_a_running_job_is_its_sessions(conn, models):
    job = await queued(conn, A.id)
    async with await store.sibling(conn) as other:
        async with conn.transaction():
            mine = await jobs_store.claim(conn)
            assert await jobs_store.claim(other) is None  # locked: skipped, not waited on
        assert mine["id"] == job
        assert await jobs_store.claim(other) is None  # running in a live session not its own
        assert (await jobs_store.claim(conn))["id"] == job  # its own, chosen again


async def test_a_job_whose_session_died_is_taken_back_and_finishes_without_redoing_work(
    conn, models, monkeypatch
):
    monkeypatch.setattr(worker, "CHUNK", 1)
    job = await queued(conn, A.id, B.id)
    dead = await store.sibling(conn)
    gone = worker.Worker(dead, CONFIG, models.gateway)
    assert await gone.step(AFTERNOON)  # A's chunk, then its process dies
    pid = (await jobs_store.get(conn, job))["backend_pid"]
    await dead.close()
    await gone.close(RuntimeError("killed"))
    for _ in range(200):  # the server ends the session soon after its connection closes
        if not await rows(conn, "SELECT 1 FROM pg_stat_activity WHERE pid = %s", pid):
            break
        await asyncio.sleep(0.05)

    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    finished = await jobs_store.get(conn, job)
    assert (finished["state"], finished["done"], finished["counts"]["classified"]) == ("done", 2, 2)
    assert asked(models, "classify", A.title) == 1 and asked(models, "classify", B.title) == 1
    assert asked(models, "analyse", A.title) == 1
    executions = await rows(conn, "SELECT * FROM runs WHERE job_id = %s ORDER BY id", job)
    assert len(executions) == 2 and executions[1]["finished_at"] is not None
    # What the dead run's calls cost counts in the job's cost, though its run never closed.
    calls = await rows(
        conn, "SELECT run_id, (usage->>'cost')::numeric AS cost FROM inference_calls"
    )
    assert {c["run_id"] for c in calls} == {e["id"] for e in executions}
    (listed,) = [r for r in await jobs_store.listing(conn, 20) if r["source"] == "job"]
    assert executions[0]["finished_at"] is None and listed["cost"] == sum(c["cost"] for c in calls)


async def test_a_job_taken_back_mid_chunk_skips_the_steps_it_had_done(conn, models):
    """Its process died after classifying the paper and before recording the chunk: the paper
    is analysed and graded, and not classified again."""
    job = await queued(conn, A.id)
    async with await store.sibling(conn) as dead:
        await jobs_store.claim(dead)
        async with model_run(dead, CONFIG, "job", gateway=models.gateway) as run:
            await nightly.classify(dead, CONFIG, run, ids=[A.id])
    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    assert asked(models, "classify", A.title) == 1 and asked(models, "analyse", A.title) == 1
    assert (await jobs_store.get(conn, job))["state"] == "done"


# -- V3: the nightly first; the most urgent job at every chunk ----------------------------------


async def test_no_chunk_starts_while_a_nightly_is_due_or_running(conn, models):
    job = await queued(conn, A.id)
    w = worker.Worker(conn, CONFIG, models.gateway)
    assert not await w.step(NIGHT)  # due at 02:00, not started
    async with await store.sibling(conn) as nightly_session:
        night = await runs.start(nightly_session, "nightly")
        assert not await w.step(NIGHT + timedelta(minutes=1))  # started, still running
        await runs.finish(nightly_session, night)
    assert (await jobs_store.get(conn, job))["state"] == "queued" and not await classified(conn)
    assert await w.step(NIGHT + timedelta(minutes=2))  # the night's run done: the job goes
    assert await classified(conn) == {A.id}


async def test_a_nightly_due_mid_job_runs_before_its_next_chunk(conn, models, monkeypatch):
    monkeypatch.setattr(worker, "CHUNK", 1)
    job = await queued(conn, A.id, B.id)
    w = worker.Worker(conn, CONFIG, models.gateway)
    assert await w.step(NIGHT - timedelta(minutes=10))  # A's chunk, before the nightly is due
    assert not await w.step(NIGHT)  # due now: the job waits
    assert (await jobs_store.get(conn, job))["done"] == 1
    await nightly.run(conn, CONFIG, fetch=False, gateway=models.gateway, with_digests=False)
    assert await classified(conn) == {A.id, B.id, C.id}  # the night took B and C
    assert await w.step(NIGHT + timedelta(minutes=30))
    finished = await jobs_store.get(conn, job)
    assert (finished["state"], finished["done"]) == ("done", 2)
    assert asked(models, "classify", B.title) == 1  # not again for the job


async def test_a_held_run_lock_holds_every_chunk(conn, models):
    job = await queued(conn, A.id)
    w = worker.Worker(conn, CONFIG, models.gateway)
    async with await store.sibling(conn) as other, run_lock(other):
        assert not await w.step(AFTERNOON)
    # Not claimed either: a job is chosen under the lock, so the oldest is never passed over.
    assert (await jobs_store.get(conn, job))["state"] == "queued" and not await classified(conn)
    assert await w.step(AFTERNOON)
    assert (await jobs_store.get(conn, job))["state"] == "done"


async def test_a_process_job_queued_during_a_longer_job_goes_at_its_next_chunk(
    conn, models, monkeypatch
):
    """A re-run (P2) stands in as a job of another kind, made here as P2 will make it."""
    await conn.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check")
    monkeypatch.setattr(worker, "CHUNK", 1)
    longer = await queued(conn, B.id, C.id, kind="rerun")
    w = worker.Worker(conn, CONFIG, models.gateway)
    assert await w.step(AFTERNOON)  # B
    process = await queued(conn, A.id)
    assert await w.step(AFTERNOON)  # A: the Process job first
    assert (await jobs_store.get(conn, process))["state"] == "done"
    assert (await jobs_store.get(conn, longer))["state"] == "running"
    assert await classified(conn) == {A.id, B.id}
    assert await w.step(AFTERNOON) and (await jobs_store.get(conn, longer))["state"] == "done"


async def test_a_job_with_tonights_run_waits_for_a_nightly_to_finish(conn, models):
    job = await jobs_store.create(conn, "process", {"papers": [A.id]}, 1, "offered")
    await jobs_store.move(conn, job, jobs_store.NOT_RUN, "tonight")
    w = worker.Worker(conn, CONFIG, models.gateway)
    assert not await w.step(AFTERNOON)
    await nightly.run(conn, CONFIG, fetch=False, gateway=models.gateway, with_digests=False)
    asked_before = asked(models, "classify", A.title)
    assert await w.step(AFTERNOON)
    assert (await jobs_store.get(conn, job))["state"] == "done"
    assert asked(models, "classify", A.title) == asked_before  # the night classified it


# -- the API --------------------------------------------------------------------------------------


@asynccontextmanager
async def api(conn, config=SCHEDULED):
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, config)
    transport = httpx.ASGITransport(app=app)
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        c.app = app
        yield c


def keys(group: list[dict]) -> list[str]:
    return [j["key"] for j in group]


async def test_jobs_are_listed_in_the_four_groups_with_the_cards_asked_for(conn, models):
    offered = await jobs_store.create(conn, "process", {"papers": [A.id]}, 1, "offered")
    running = await queued(conn, B.id)
    await jobs_store.claim(conn)
    failed = await jobs_store.create(conn, "process", {"papers": [C.id]}, 1, "failed")
    gone = await jobs_store.create(conn, "process", {"papers": ["2610.00009"]}, 1, "discarded")
    (card,) = await rows(
        conn,
        "INSERT INTO fulltext_passes (paper_id, paper_revision, kind, reason, priority) "
        "VALUES (%s, 1, 'deep', 'request', 0) RETURNING id",
        A.id,
    )
    async with api(conn) as http:
        listed = (await http.get("/api/jobs")).json()
    assert keys(listed["not_run"]) == [f"job:{offered}"]
    assert keys(listed["running"]) == [f"job:{running}", f"card:{card['id']}"]
    assert keys(listed["failed"]) == [f"job:{failed}"] and listed["done"] == []
    assert f"job:{gone}" not in str(listed)
    (row,) = listed["not_run"]
    assert row["paper"] == {"id": A.id, "title": A.title}
    assert row["actions"] == ["run", "tonight", "discard"]
    assert listed["failed"][0]["actions"] == ["again"]
    assert listed["running"][1]["actions"] == [] and listed["running"][1]["kind"] == "deep"


@pytest.mark.parametrize(
    ("action", "state", "group"),
    [("run", "queued", "running"), ("tonight", "tonight", "running"), ("discard", None, None)],
)
async def test_a_job_not_run_is_run_now_put_on_tonights_run_or_discarded(
    conn, models, action, state, group
):
    job = await jobs_store.create(conn, "process", {"papers": [A.id]}, 1, "offered")
    async with api(conn) as http:
        acted = await http.post(f"/api/jobs/{job}/{action}")
        again = await http.post(f"/api/jobs/{job}/{action}")
        missing = await http.post(f"/api/jobs/{job + 99}/{action}")
    assert acted.status_code == 200 and (await jobs_store.get(conn, job))["state"] == (
        state or "discarded"
    )
    listed = acted.json()
    assert keys(listed[group]) == [f"job:{job}"] if group else not any(listed.values())
    assert (again.status_code, missing.status_code) == (409, 404)


async def test_a_failed_job_is_tried_again(conn, models):
    job = await jobs_store.create(conn, "process", {"papers": [A.id]}, 1, "failed")
    async with api(conn) as http:
        refused = await http.post(f"/api/jobs/{job}/run")
        acted = await http.post(f"/api/jobs/{job}/again")
    assert refused.status_code == 409 and refused.json()["detail"] == "the job is failed now"
    assert keys(acted.json()["running"]) == [f"job:{job}"]


async def test_process_fetches_the_paper_and_queues_its_job_once(conn, models, monkeypatch):
    elsewhere = paper("2610.09999", title="Elsewhere")
    monkeypatch.setattr(
        misses, "fetch_by_ids", answering(lambda ids: [elsewhere] if elsewhere.id in ids else [])
    )
    async with api(conn) as http:
        first = await http.post("/api/jobs/process", json={"paper_id": "2610.09999v2"})
        again = await http.post("/api/jobs/process", json={"paper_id": elsewhere.id})
        page = (await http.get(f"/api/papers/{elsewhere.id}")).json()
        missing = await http.post("/api/jobs/process", json={"paper_id": "2601.99999"})
        assert http.app.state.jobs_wake.is_set()
    assert first.status_code == 202 and first.json()["state"] == "queued"
    assert again.json()["id"] == first.json()["id"]  # asked again, the same job
    assert (page["stored"], page["in_collection"]) == (True, False)
    assert page["process"] == {"id": first.json()["id"], "state": "queued", "error": None}
    assert missing.status_code == 404

    await work_out(worker.Worker(conn, CONFIG, models.gateway))
    assert await classified(conn) == {elsewhere.id}
    async with api(conn) as http:
        page = (await http.get(f"/api/papers/{elsewhere.id}")).json()
        processed = await http.post("/api/jobs/process", json={"paper_id": elsewhere.id})
        listed = (await http.get("/api/jobs")).json()
    assert page["in_collection"] and page["process"]["state"] == "done"
    assert processed.status_code == 409
    assert keys(listed["done"]) == [f"job:{first.json()['id']}"]
    assert listed["done"][0]["cost"] > 0


async def test_process_holds_its_job_so_a_try_again_meanwhile_waits_and_is_refused(
    conn, models, monkeypatch
):
    job = await jobs_store.create(conn, "process", {"papers": [A.id]}, 1, "failed")
    move, page = jobs_store.move, None
    async with await store.sibling(conn) as other:

        async def raced(c, *args):
            nonlocal page  # the Jobs page's Try again comes in while Process decides
            page = asyncio.create_task(move(other, job, ("failed",), "queued"))
            await asyncio.sleep(0.3)
            return await move(c, *args)

        monkeypatch.setattr(jobs_store, "move", raced)
        async with conn.transaction():
            processed = await jobs_store.process(conn, A.id)
        assert await page is None  # it waited, then found the job queued
    assert processed["id"] == job and processed["state"] == "queued"


async def test_the_paper_page_reads_the_job_before_the_paper(conn, models, monkeypatch):
    """The job ends while the page is read: the page says it still runs, so it is read again."""
    job = await queued(conn, A.id)
    await jobs_store.claim(conn)
    read = api_reading.reading.paper

    async def ended_meanwhile(c, paper_id):
        found = await read(c, paper_id)
        await jobs_store.finish(conn, job, "done")
        return found

    monkeypatch.setattr(api_reading.reading, "paper", ended_meanwhile)
    async with api(conn) as http:
        page = (await http.get(f"/api/papers/{A.id}")).json()
    assert page["process"]["state"] == "running"


async def test_with_the_schedule_off_process_only_fetches_the_paper_for_the_next_run(
    conn, models, monkeypatch
):
    elsewhere = paper("2610.09999", title="Elsewhere")
    monkeypatch.setattr(misses, "fetch_by_ids", answering(lambda ids: [elsewhere]))
    async with api(conn, CONFIG) as http:
        fetched = await http.post("/api/jobs/process", json={"paper_id": elsewhere.id})
        page = (await http.get(f"/api/papers/{elsewhere.id}")).json()
    assert (fetched.status_code, fetched.json()) == (202, None)
    assert (page["stored"], page["next_run"], page["process"]) == (True, True, None)
    assert await rows(conn, "SELECT 1 FROM jobs") == []


async def test_process_on_a_dismissed_paper_is_refused_and_keeps_nothing(conn, models):
    await papers_store.dismiss(conn, A.id)
    async with api(conn) as http:
        refused = await http.post("/api/jobs/process", json={"paper_id": A.id})
    assert refused.status_code == 409 and await rows(conn, "SELECT 1 FROM jobs") == []
