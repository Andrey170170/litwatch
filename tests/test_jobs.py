"""The owner's queue (docs/plans/jobs.md, P1): the worker runs a job's papers, and only those,
through classification, analysis and grading, a chunk at a time under the run lock; two workers
never run one job, and a job whose chunk's item was taken back goes on; no chunk starts while a
nightly run is due or running; `/api/jobs` lists and acts on jobs, and Process queues one. Each
chunk runs as a work item through a Work gate (`tests.working`). Models are scripted
(`test_steps.Scripted`)."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial

import psycopg
import pytest

from litwatch.api import create_app
from litwatch.api import jobs as jobs_api
from litwatch.api import reading as api_reading
from litwatch.core import Cause, Depth, Status
from litwatch.core.fulltext import REQUEST_PRIORITY, TEXT_REQUEST_PRIORITY
from litwatch.feedback import misses
from litwatch.gates.database import DatabaseGate
from litwatch.gates.work import Later, WorkGate
from litwatch.jobs import nightly, worker
from litwatch.jobs.context import UnclosedError, model_run
from litwatch.llm import ModelGate, prices
from litwatch.llm.context import spending
from litwatch.store import failures, runs
from litwatch.store import jobs as jobs_store
from litwatch.store import papers as papers_store
from litwatch.store import work as queue
from litwatch.store.calls import CallJournal
from tests import sessions, settling
from tests.conftest import AGENTS, TREE, AppClient, answering, lease_lapses, paper, started
from tests.fakes import FAST, BatchingProvider, ScriptedProvider
from tests.scripted import CONFIG, Scripted, decision
from tests.sessions import Session, in_read, in_write, staged
from tests.working import (
    QUIET,
    chunks,
    job_of,
    killed,
    nightly_done,
    nightly_due,
    on_tonight,
    step,
    stranded,
    work_out,
)

SCHEDULED = CONFIG.model_copy(update={"schedule": QUIET})  # the worker runs, no fire near

A = paper("2610.00001", title="Probes for hidden steps", abstract="Linear probes on activations.")
B = paper("2610.00002", title="Is the chain of thought faithful?", abstract="We test traces.")
C = paper("2610.00003", title="Agents that plan", abstract="Tool use over many steps.")

TOKEN = "s3cret"


@pytest.fixture
async def models(conn: Session):
    await settling.save_tree(conn, TREE)
    await in_write(conn, papers_store.upsert, [A, B, C])
    return Scripted()


async def queued(conn: Session, *ids: str, kind: str = "process") -> int:
    return await in_write(conn, jobs_store.create, kind, {"papers": list(ids)}, len(ids), "queued")


async def classified(conn: Session) -> set[str]:
    rows = await (await conn.execute("SELECT DISTINCT paper_id FROM classifications")).fetchall()
    return {r["paper_id"] for r in rows}


async def rows(conn: Session, query: str, *params) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


def asked(models: Scripted, role: str, title: str) -> int:
    return models.asked.count((role, title))


# -- V1: a job runs its papers through the steps, and only those ---------------------------------


async def test_a_process_job_runs_its_paper_through_every_step_and_no_other(
    conn: Session, db, models
):
    job = await queued(conn, A.id)
    w = worker.JobWorker(db, CONFIG, models.gate(CONFIG))
    assert await work_out(w) == 1

    assert await classified(conn) == {A.id}  # B and C are due a nightly, and wait for it
    analysed = await rows(conn, "SELECT paper_id FROM analyses")
    assert [r["paper_id"] for r in analysed] == [A.id]
    passes = await rows(conn, "SELECT paper_id FROM grading_passes")
    assert [r["paper_id"] for r in passes] == [A.id]
    assert {t for t, title in models.asked if title != A.title} == set()
    done = await in_read(conn, jobs_store.get, job)
    assert (done["state"], done["done"], done["error"]) == ("done", 1, None)
    assert done["counts"]["classified"] == 1 and done["counts"]["analysed"] == 1
    # One run of kind `job`, closed, with the job's cost; the Nights table reads nightly runs only.
    (run,) = await rows(conn, "SELECT * FROM runs WHERE job_id = %s", job)
    assert (run["kind"], run["error"]) == ("job", None) and run["finished_at"] is not None
    assert run["cost"] > 0 and run["counts"]["classified"] == 1
    await nightly.run(db, CONFIG, fetch=False, models=models.gate(CONFIG), with_digests=False)
    async with api(conn) as http:
        nights = (await http.get("/api/settings/runs")).json()
    assert [r["kind"] for r in nights] == ["nightly"]


async def test_a_job_of_many_chunks_is_one_run(conn: Session, db, models, monkeypatch):
    monkeypatch.setattr(worker, "CHUNK", 1)
    job = await queued(conn, A.id, B.id)
    w = worker.JobWorker(db, CONFIG, models.gate(CONFIG))
    assert await work_out(w) == 2
    assert await classified(conn) == {A.id, B.id}
    (run,) = await rows(conn, "SELECT * FROM runs WHERE job_id = %s", job)
    assert run["counts"]["classified"] == 2 and run["finished_at"] is not None
    assert (await in_read(conn, jobs_store.get, job))["state"] == "done"


async def test_a_job_whose_paper_failed_a_step_fails_and_says_where(conn: Session, db, models):
    models.decisions[A.title] = {"broken": True}
    job = await queued(conn, A.id)
    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    failed = await in_read(conn, jobs_store.get, job)
    assert (failed["state"], failed["error"]) == ("failed", "classification failed on 1")

    # Try again starts it over: what is still due runs, nothing else.
    del models.decisions[A.title]
    assert (await in_write(conn, jobs_store.move, job, ("failed",), "queued"))["error"] is None
    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    again = await in_read(conn, jobs_store.get, job)
    assert (again["state"], again["counts"]["classified"]) == ("done", 1)


async def test_a_job_tried_again_on_a_step_waiting_for_the_owner_fails_again(
    conn: Session, db, models
):
    """Three runs in a row failed, the step waits for the owner on Needs you: Try again has
    nothing due and fails again, saying so, until the owner acts there; then the job goes back
    in the queue by itself and does what is left."""
    models.decisions[A.title] = {"broken": True}
    job = await queued(conn, A.id)
    for _ in range(3):
        await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
        await in_write(conn, jobs_store.move, job, ("failed",), "queued")
    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    stuck = await in_read(conn, jobs_store.get, job)
    assert stuck["state"] == "failed"
    assert stuck["error"] == "classification failed on 1 (waits on Needs you)"
    assert asked(models, "classify", A.title) == 3

    del models.decisions[A.title]
    async with settling.change(
        conn, A.id, cause=Cause.OWNER
    ) as change:  # as Needs you's own retry does
        await failures.succeeded(change.unit, A.id, "classification")
    assert (await in_read(conn, jobs_store.get, job))["state"] == "queued"
    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    assert (await in_read(conn, jobs_store.get, job))["state"] == "done"
    assert asked(models, "classify", A.title) == 4


async def test_a_failure_the_owner_clears_while_the_job_ends_still_settles_it(
    conn: Session, db, models, monkeypatch
):
    models.decisions[A.title] = {"broken": True}
    job = await queued(conn, A.id)
    read, owner = failures.open_on, None
    async with await sessions.sibling(conn) as other:

        async def clear():  # Needs you's retry, from another request
            async with settling.change(other, A.id, cause=Cause.OWNER) as change:
                await failures.succeeded(change.unit, A.id, "classification")

        async def raced(*args):
            nonlocal owner
            found = await read(*args)
            owner = settling.apart(clear())
            await asyncio.sleep(0.3)
            return found

        monkeypatch.setattr(failures, "open_on", raced)
        assert await step(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
        await owner
    assert (await in_read(conn, jobs_store.get, job))["state"] == "queued"  # failed, then settled


async def test_a_failure_the_owner_clears_before_the_job_ends_runs_the_step_again(
    conn: Session, db, models, monkeypatch
):
    """Analysis fails; while the job grades, the owner's Try again clears the failure. The job
    does not end done with the analysis left: it goes back in the queue and does it."""
    models.analyses[A.title] = {"broken": True}
    grade = nightly.grade

    async def owner_retries(*args, **kwargs):
        async with (
            await sessions.sibling(conn) as owner,
            settling.change(owner, A.id, cause=Cause.OWNER) as change,
        ):
            await failures.succeeded(change.unit, A.id, "analysis")  # as Needs you's Try again
        del models.analyses[A.title]
        return await grade(*args, **kwargs)

    monkeypatch.setattr(nightly, "grade", owner_retries)
    job = await queued(conn, A.id)
    w = worker.JobWorker(db, CONFIG, models.gate(CONFIG))
    assert await step(w)
    assert (await in_read(conn, jobs_store.get, job))["state"] == "queued"
    monkeypatch.setattr(nightly, "grade", grade)
    await work_out(w)
    assert (await in_read(conn, jobs_store.get, job))["state"] == "done"
    assert asked(models, "analyse", A.title) == 2


async def test_a_failed_job_put_back_by_a_settle_gives_its_passes_the_owners_priorities(
    conn: Session, db, models
):
    """The settle that clears the paper's last failure puts its job back first, then gives the
    passes it queued the owner's priorities, in one go."""
    models.decisions[A.title] = decision("cot-faithfulness", **{"cot-faithfulness": 0.9})
    models.fits = {(r, A.title): {"reason": "Core.", "fit": "central"} for r in ("judge", "check")}
    await nightly.run(db, CONFIG, fetch=False, models=models.gate(CONFIG), with_digests=False)
    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "failed")
    await conn.execute(
        "INSERT INTO step_failures (paper_id, step, topic, paper_revision, failed_runs, last_error)"
        " VALUES (%s, 'analysis', '', 1, 3, 'unusable')",
        (A.id,),
    )
    async with settling.change(
        conn, A.id, cause=Cause.OWNER
    ) as change:  # as Needs you's own retry does
        await failures.succeeded(change.unit, A.id, "analysis")
    assert (await in_read(conn, jobs_store.get, job))["state"] == "queued"
    passes = await rows(conn, "SELECT * FROM fulltext_passes WHERE paper_id = %s", A.id)
    assert {(p["kind"], p["priority"]) for p in passes} == {
        ("brief", TEXT_REQUEST_PRIORITY),
        ("deep", REQUEST_PRIORITY),
    }


async def test_a_failed_job_settles_when_the_topic_it_failed_grading_on_is_paused(
    conn: Session, models
):
    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "failed")
    await conn.execute(
        "INSERT INTO step_failures (paper_id, step, topic, paper_revision, failed_runs, last_error)"
        " VALUES (%s, 'grading', 'cot-faithfulness', 1, 1, 'unusable')",
        (A.id,),
    )
    paused = [
        t.model_copy(update={"status": Status.PAUSED}) if t.depth == Depth.FOCUS else t
        for t in TREE.topics
    ]
    await settling.save_taxonomy(conn, TREE.model_copy(update={"topics": paused}))
    assert (await in_read(conn, jobs_store.get, job))[
        "state"
    ] == "queued"  # nothing waits on it now


async def test_a_job_answers_a_call_a_dead_nightly_left_in_an_ended_batch_from_it(
    conn: Session, db, models
):
    """The nightly batched the paper's analysis and died before reading it: the job reads the
    ended batch rather than paying for the call again."""
    models.decisions[A.title] = decision("cot-faithfulness", **{"cot-faithfulness": 0.9})
    models.fits = {(r, A.title): {"reason": "Core.", "fit": "central"} for r in ("judge", "check")}
    anthropic = BatchingProvider("anthropic", models.respond)
    openrouter = ScriptedProvider("openrouter", models.respond)
    providers = {"anthropic": anthropic, "openrouter": openrouter}

    def gate() -> ModelGate:  # each process's own
        journal = CallJournal(db)
        return ModelGate(CONFIG.models, providers, journal, retry_delays=(0.0,), batch_options=FAST)

    with spending(deadline=datetime.now(UTC) + timedelta(hours=1)):  # as the nightly's
        async with model_run(db, "nightly", models=gate()) as night:
            await nightly.classify(db, CONFIG, night, ids=[A.id])
            await nightly.analyse(db, CONFIG, night, ids=[A.id])
    assert anthropic.batched and not anthropic.calls
    await conn.execute("DELETE FROM analyses")  # as if it died before reading the batch
    await conn.execute(
        "UPDATE inference_calls SET output = NULL, usage = usage - 'cost', responded_at = NULL"
    )

    job = await queued(conn, A.id)
    await work_out(worker.JobWorker(db, CONFIG, gate()))
    assert (await in_read(conn, jobs_store.get, job))["state"] == "done"
    assert [r for r, _ in anthropic.calls if r.model == "test-analyse"] == []  # not paid again


async def test_a_job_whose_run_cannot_start_fails_and_says_why(
    conn: Session, db, models, monkeypatch
):
    """Its gateway cannot be built (an unreadable key file, say): the job fails with the error,
    rather than the worker failing, taking it back and trying it again forever."""

    def unreadable(*_, **__):
        raise OSError("the key file is unreadable")

    monkeypatch.setattr(worker, "model_run", unreadable)
    job = await queued(conn, A.id)
    assert await step(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    failed = await in_read(conn, jobs_store.get, job)
    assert (failed["state"], failed["error"]) == ("failed", "OSError: the key file is unreadable")


async def test_a_job_whose_paper_is_dismissed_while_it_runs_is_discarded_however_it_ends(
    conn: Session, db, models, monkeypatch
):
    async def dismissed_then_broken(*_, **__):
        async with (
            await sessions.sibling(conn) as owner,
            settling.change(owner, A.id, cause=Cause.OWNER) as change,
        ):
            await papers_store.dismiss(change.unit, A.id)
        raise RuntimeError("the store went away")

    monkeypatch.setattr(nightly, "analyse", dismissed_then_broken)
    job = await queued(conn, A.id)
    assert await step(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    assert (await in_read(conn, jobs_store.get, job))["state"] == "discarded"


async def test_a_failed_job_whose_paper_the_owner_dismisses_is_discarded(conn: Session, db, models):
    models.decisions[A.title] = {"broken": True}
    job = await queued(conn, A.id)
    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    async with settling.change(conn, A.id, cause=Cause.OWNER) as change:
        await papers_store.dismiss(change.unit, A.id)
    assert (await in_read(conn, jobs_store.get, job))["state"] == "discarded"


async def test_a_process_jobs_passes_go_as_the_owners_requests(conn: Session, db, models):
    models.decisions[A.title] = decision("cot-faithfulness", **{"cot-faithfulness": 0.9})
    models.fits = {(r, A.title): {"reason": "Core.", "fit": "central"} for r in ("judge", "check")}
    await queued(conn, A.id)
    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    passes = await rows(conn, "SELECT * FROM fulltext_passes WHERE paper_id = %s", A.id)
    assert {p["kind"] for p in passes} == {"brief", "deep"}
    for p in passes:
        first = REQUEST_PRIORITY if p["kind"] == "deep" else TEXT_REQUEST_PRIORITY
        assert p["priority"] == first, p


async def test_a_process_jobs_paper_the_nightly_took_first_still_goes_as_the_owners(
    conn: Session, db, models
):
    """Process while the nightly is due: the night processes the paper, its passes the owner's."""
    models.decisions[A.title] = decision("cot-faithfulness", **{"cot-faithfulness": 0.9})
    models.fits = {(r, A.title): {"reason": "Core.", "fit": "central"} for r in ("judge", "check")}
    await queued(conn, A.id)
    await nightly.run(db, CONFIG, fetch=False, models=models.gate(CONFIG), with_digests=False)
    passes = await rows(conn, "SELECT * FROM fulltext_passes WHERE paper_id = %s", A.id)
    assert {(p["kind"], p["priority"]) for p in passes} == {
        ("brief", TEXT_REQUEST_PRIORITY),
        ("deep", REQUEST_PRIORITY),
    }


async def test_a_job_that_raises_fails_with_its_error_and_its_run_records_it(
    conn: Session, db, models, monkeypatch
):
    async def broken(*_, **__):
        raise RuntimeError("the store went away")

    monkeypatch.setattr(nightly, "analyse", broken)
    job = await queued(conn, A.id)
    assert await step(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    failed = await in_read(conn, jobs_store.get, job)
    assert (failed["state"], failed["error"]) == ("failed", "RuntimeError: the store went away")
    (run,) = await rows(conn, "SELECT * FROM runs WHERE job_id = %s", job)
    assert run["error"] == "RuntimeError: the store went away" and run["finished_at"]


# -- V2: one job, one worker; a dead job is taken back --------------------------------------------


async def test_two_workers_never_run_one_job(conn: Session, db, models):
    job = await queued(conn, A.id)
    async with DatabaseGate(conn.test_dsn, conn.test_schema) as other:  # another process
        first = worker.JobWorker(db, CONFIG, models.gate(CONFIG))
        second = worker.JobWorker(other, CONFIG, models.gate(CONFIG))
        ran = await asyncio.gather(step(first), step(second))
        await second.close(RuntimeError("test over"))
    assert sorted(ran) == [False, True]
    assert asked(models, "classify", A.title) == 1
    assert (await in_read(conn, jobs_store.get, job))["state"] == "done"


async def test_a_process_job_taken_back_after_its_chunk_does_what_the_owner_left_due(
    conn: Session, db, models, monkeypatch
):
    """Its process died after recording its chunk, before ending it; meanwhile the owner's Try
    again left analysis due. Taken back, it starts over from its paper and does it."""
    models.analyses[A.title] = {"broken": True}
    job = await queued(conn, A.id)
    async with DatabaseGate(conn.test_dsn, conn.test_schema) as dead:  # another process's
        gone = worker.JobWorker(dead, CONFIG, models.gate(CONFIG))
        await killed(gone, monkeypatch, "_end")
        await gone.close(RuntimeError("killed"))
    assert (await in_read(conn, jobs_store.get, job))["done"] == 1
    del models.analyses[A.title]
    async with settling.change(conn, A.id, cause=Cause.OWNER) as change:  # as Needs you's Try again
        await failures.succeeded(change.unit, A.id, "analysis")

    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    assert (await in_read(conn, jobs_store.get, job))["state"] == "done"
    assert asked(models, "analyse", A.title) == 2


async def test_a_job_whose_process_died_is_taken_back_and_finishes_without_redoing_work(
    conn: Session, db, models, monkeypatch
):
    """Killed as its second chunk starts: another process's worker goes on with the job on a
    run of its own, redoing nothing. The dead run's calls count in the job's cost."""
    monkeypatch.setattr(worker, "CHUNK", 1)
    job = await queued(conn, A.id, B.id)
    dead = DatabaseGate(conn.test_dsn, conn.test_schema)  # another process's
    await dead.open()
    gone = worker.JobWorker(dead, CONFIG, models.gate(CONFIG))
    assert await step(gone)  # A's chunk
    await killed(gone, monkeypatch, "_chunk")

    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    finished = await in_read(conn, jobs_store.get, job)
    assert (finished["state"], finished["done"], finished["counts"]["classified"]) == ("done", 2, 2)
    assert asked(models, "classify", A.title) == 1 and asked(models, "classify", B.title) == 1
    assert asked(models, "analyse", A.title) == 1
    executions = await rows(conn, "SELECT * FROM runs WHERE job_id = %s ORDER BY id", job)
    assert len(executions) == 2 and executions[1]["finished_at"] is not None
    calls = await rows(
        conn, "SELECT run_id, (usage->>'cost')::numeric AS cost FROM inference_calls"
    )
    assert {c["run_id"] for c in calls} == {e["id"] for e in executions}
    (listed,) = [r for r in await in_read(conn, jobs_store.listing, 20, 3) if r["source"] == "job"]
    assert executions[0]["finished_at"] is None and listed["cost"] == sum(c["cost"] for c in calls)
    await gone.close(RuntimeError("killed"))
    await dead.close()


async def test_a_process_job_started_over_after_the_owner_acted_is_not_asked_again(
    conn: Session, db
):
    """The owner acted on its paper while it ran (`settle_process`), then its process died: the
    claim that starts it over from its paper does what the owner left due, so its end is done,
    not queued to run once more."""
    job = await queued(conn, A.id)
    await in_write(conn, jobs_store.claim, job, afresh=True)
    async with settling.change(conn, A.id, cause=Cause.OWNER) as change:  # a Try again
        await jobs_store.settle_process(change.unit, A.id, owner=True)
    await in_write(conn, jobs_store.claim, job, afresh=True)  # taken back, started over
    assert await in_write(conn, jobs_store.finish, job, "done")
    assert (await job_of(conn, job))["state"] == "done"


async def test_a_chunk_item_taken_back_keeps_which_runs_it_ran(
    conn: Session, db, models, monkeypatch
):
    """Its process died in the job's first chunk; taken back, the same item runs the chunk on a
    run of its own. Each run names the item that ran it, the item its latest run."""
    job = await queued(conn, A.id)
    async with DatabaseGate(conn.test_dsn, conn.test_schema) as dead:  # another process's
        gone = worker.JobWorker(dead, CONFIG, models.gate(CONFIG))
        await killed(gone, monkeypatch, "_chunk")
        await gone.close(RuntimeError("killed"))
    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    (item,) = await rows(conn, "SELECT * FROM work_items WHERE kind = %s", jobs_store.WORK)
    first, again = await rows(conn, "SELECT * FROM runs WHERE job_id = %s ORDER BY id", job)
    assert (first["work_id"], again["work_id"]) == (item["id"], item["id"])
    assert item["run_id"] == again["id"] and (await job_of(conn, job))["run_id"] == again["id"]


async def test_a_job_taken_back_mid_chunk_skips_the_steps_it_had_done(conn: Session, db, models):
    """Its process died after classifying the paper and before recording the chunk: the paper
    is analysed and graded, and not classified again."""
    job = await queued(conn, A.id)
    async with DatabaseGate(conn.test_dsn, conn.test_schema) as dead:  # another process's
        async with dead.write("claim") as unit:
            await jobs_store.claim(unit, job, afresh=True)
        async with model_run(dead, "job", models=models.gate(CONFIG)) as run:
            await nightly.classify(dead, CONFIG, run, ids=[A.id])
    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    assert asked(models, "classify", A.title) == 1 and asked(models, "analyse", A.title) == 1
    assert (await in_read(conn, jobs_store.get, job))["state"] == "done"


async def test_a_chunk_that_loses_the_database_is_tried_again_by_the_gate(
    conn: Session, db, models, monkeypatch
):
    """The database goes away under a chunk's step: the error reaches the gate, which puts the
    item back, and the job goes on in the same run, not failed."""
    job = await queued(conn, A.id)
    grade, lost = nightly.grade, []

    async def drops(*args, **kwargs):
        if not lost:
            lost.append(True)
            raise psycopg.OperationalError("the server closed the connection unexpectedly")
        return await grade(*args, **kwargs)

    monkeypatch.setattr(nightly, "grade", drops)
    found = await chunks(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    assert isinstance(found[0], psycopg.OperationalError) and found[1:] == [None]
    finished = await job_of(conn, job)
    assert (finished["state"], finished["error"]) == ("done", None)
    # What the cut-off attempt did before the loss counts once its retry records the chunk.
    counted = {k: finished["counts"].get(k) for k in ("classified", "analysed")}
    assert counted == {"classified": 1, "analysed": 1}
    (run,) = await rows(conn, "SELECT * FROM runs WHERE job_id = %s", job)
    assert run["error"] is None and run["finished_at"] is not None


@pytest.mark.parametrize("recorded", [True, False])
async def test_a_chunk_whose_first_look_fails_fails_its_job_or_keeps_its_chunk(
    conn: Session, db, models, monkeypatch, recorded
):
    """The chunk's read before the run lock raises (a statement failed): the job fails, as for a
    chunk's own error, rather than left runnable with its item ended; when that failure cannot
    be recorded either, the chunk is kept, to look again. Never a job with no chunk."""
    monkeypatch.setattr(worker, "WAIT", timedelta(0))
    job = await queued(conn, A.id)
    real_waits, real_fail, raised = jobs_store.nightly_waits, jobs_store.fail, []

    async def waits(unit, **kwargs):
        if not raised:
            raised.append(True)
            raise RuntimeError("the read failed")
        return await real_waits(unit, **kwargs)

    async def fail(unit, *args, **kwargs):
        if not recorded:
            raise RuntimeError("the failure is not recorded")
        return await real_fail(unit, *args, **kwargs)

    monkeypatch.setattr(jobs_store, "nightly_waits", waits)
    monkeypatch.setattr(jobs_store, "fail", fail)
    (ended,) = await chunks(worker.JobWorker(db, CONFIG, models.gate(CONFIG)), limit=1)
    assert await stranded(conn) == []
    if recorded:
        assert isinstance(ended, RuntimeError)
        assert (await job_of(conn, job))["state"] == "failed"
    else:
        assert isinstance(ended, Later) and (await job_of(conn, job))["state"] == "queued"
        assert await step(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))  # it looks again
        assert (await job_of(conn, job))["state"] == "done"


@pytest.mark.parametrize("op", ["release", "wake", "link"])
async def test_scheduling_a_chunk_takes_its_jobs_row_before_its_item(conn: Session, db, op):
    """The nightly's release, its wake and a chunk linking its run each take the job's row
    before its item's, as every write to both does: held at the job's row, it holds nothing of
    the item yet."""
    job = await queued(conn, A.id)
    await conn.execute(  # waiting on a nightly
        "UPDATE work_items SET not_before = now() + interval '1 hour' WHERE kind = %s",
        (jobs_store.WORK,),
    )
    (row,) = await rows(conn, "SELECT * FROM work_items WHERE kind = %s", jobs_store.WORK)
    run = await started(conn, "job", job_id=job)
    async with db.read("the item") as unit:
        item = await queue.get(unit, row["id"])
    assert item is not None
    probe = await sessions.sibling(conn)
    holding, let_go = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with sessions.Bridge(conn).write("hold the job's row") as unit:
            await unit.execute("SELECT 1 FROM jobs WHERE id = %s FOR UPDATE", (job,))
            holding.set()
            await let_go.wait()

    async def schedule() -> None:
        locks = ["tonight"] if op == "release" else []
        async with db.write(f"the {op}", locks=locks) as unit:
            if op == "link":
                await jobs_store.link(unit, job, item, run)
            else:
                await getattr(jobs_store, op)(unit)

    holder = asyncio.create_task(hold())
    try:
        await asyncio.wait_for(holding.wait(), 10)
        scheduling = asyncio.create_task(schedule())
        await asyncio.sleep(0.5)
        assert not scheduling.done()  # it waits for the job's row
        await probe.execute(  # the item's row is free
            "SELECT 1 FROM work_items WHERE id = %s FOR UPDATE NOWAIT", (row["id"],)
        )
    finally:
        let_go.set()
        await holder
        await probe.close()
    await asyncio.wait_for(scheduling, 10)
    (after,) = await rows(conn, "SELECT * FROM work_items WHERE id = %s", row["id"])
    if op != "link":
        assert after["not_before"] <= datetime.now(UTC)  # brought forward


async def test_a_running_job_is_alive_while_its_chunk_holds_its_lease(
    conn: Session, db, models, monkeypatch
):
    """The job's run reads alive by its chunk's item's lease, and dead once the lease lapses."""
    job = await queued(conn, A.id)
    started_, hold = asyncio.Event(), asyncio.Event()
    classify = nightly.classify

    async def holding(*args, **kwargs):
        started_.set()
        await hold.wait()
        return await classify(*args, **kwargs)

    async def alive(run_id: int) -> bool:
        query = f"SELECT {runs.ALIVE} AS alive FROM runs r WHERE r.id = %s"
        (row,) = await rows(conn, query, run_id)
        return row["alive"]

    async def lease(by: str) -> None:
        await conn.execute(
            f"UPDATE work_items SET lease_until = now() {by} interval '1 hour' WHERE kind = 'job'"
        )

    monkeypatch.setattr(nightly, "classify", holding)
    task = asyncio.create_task(chunks(worker.JobWorker(db, CONFIG, models.gate(CONFIG))))
    await asyncio.wait_for(started_.wait(), 10)
    (run,) = await rows(conn, "SELECT * FROM runs WHERE job_id = %s", job)
    assert await alive(run["id"])
    await lease("-")
    assert not await alive(run["id"])
    await lease("+")
    hold.set()
    await task
    assert not await alive(run["id"])  # its item ended
    assert (await job_of(conn, job))["state"] == "done"


# -- V3: the nightly first; the most urgent job at every chunk ----------------------------------


async def test_no_chunk_starts_while_a_nightly_is_due_or_running(
    conn: Session, db, models, monkeypatch
):
    """A scheduled nightly's item waits: the chunk waits too. The CLI's nightly runs, alive by
    its item's lease: the chunk waits. Its lease lapses (its process died): the chunk goes."""
    monkeypatch.setattr(worker, "WAIT", timedelta(0))
    job = await queued(conn, A.id)
    w = worker.JobWorker(db, SCHEDULED, models.gate(SCHEDULED))
    await nightly_due(conn)
    assert not await step(w)  # due, not started
    await nightly_done(conn)
    night = await started(conn, "nightly", alive=True)
    assert not await step(w)  # started, still running
    assert (await in_read(conn, jobs_store.get, job))["state"] == "queued" and not await classified(
        conn
    )
    await lease_lapses(conn, night)
    assert await step(w)  # the night's run is dead: the job goes
    assert await classified(conn) == {A.id}


async def test_a_nightly_due_mid_job_runs_before_its_next_chunk(
    conn: Session, db, models, monkeypatch
):
    """The nightly's end releases the chunk that waited for it, without waiting `WAIT`."""
    monkeypatch.setattr(worker, "CHUNK", 1)
    job = await queued(conn, A.id, B.id)
    w = worker.JobWorker(db, SCHEDULED, models.gate(SCHEDULED))
    assert await step(w)  # A's chunk, before the nightly is due
    await nightly_due(conn)
    (waited,) = await chunks(w, limit=1)  # due now: the job waits
    assert isinstance(waited, Later)
    assert (await in_read(conn, jobs_store.get, job))["done"] == 1
    await nightly_done(conn)
    await nightly.run(db, CONFIG, fetch=False, models=models.gate(CONFIG), with_digests=False)
    assert await classified(conn) == {A.id, B.id, C.id}  # the night took B and C
    assert await step(w)  # due at once: the nightly's end submitted it
    finished = await in_read(conn, jobs_store.get, job)
    assert (finished["state"], finished["done"]) == ("done", 2)
    assert asked(models, "classify", B.title) == 1  # not again for the job


async def test_a_chunk_claimed_as_a_fire_is_published_waits_for_that_nightly(
    conn: Session, db, models
):
    """The scheduler publishes a nightly fire under the `fire` lock, not committed yet, as the
    chunk comes to its claim: the claim waits for the lock, then sees the fire's item, and the
    chunk waits for that nightly, not claimed."""
    job = await queued(conn, A.id)
    published, commit = asyncio.Event(), asyncio.Event()

    async def publish() -> None:
        async with sessions.Bridge(conn).write("schedule: nightly", locks=["fire"]) as unit:
            await queue.submit(unit, "nightly", "a fire", lasting=True)
            published.set()
            await commit.wait()

    publisher = asyncio.create_task(publish())
    await asyncio.wait_for(published.wait(), 10)
    stepping = asyncio.create_task(
        chunks(worker.JobWorker(db, SCHEDULED, models.gate(SCHEDULED)), 1)
    )
    await asyncio.sleep(0.5)  # the chunk comes to its claim and waits on the lock
    commit.set()
    await publisher
    (waited,) = await asyncio.wait_for(stepping, 10)
    assert isinstance(waited, Later)
    assert (await job_of(conn, job))["state"] == "queued" and not await classified(conn)


async def test_a_chunk_waits_for_a_nightly_fire_due_but_not_published_yet(
    conn: Session, db, models
):
    """The nightly fired two minutes ago and the scheduler has not submitted its item yet (it
    is about to, or late): the chunk counts it as a nightly that waits."""
    fired = datetime.now(CONFIG.schedule.zone) - timedelta(minutes=2)
    cron = f"{fired.minute} {fired.hour} * * *"
    due = CONFIG.model_copy(update={"schedule": QUIET.model_copy(update={"nightly": cron})})
    job = await queued(conn, A.id)
    (waited,) = await chunks(worker.JobWorker(db, due, models.gate(due)), limit=1)
    assert isinstance(waited, Later)
    assert (await job_of(conn, job))["state"] == "queued" and not await classified(conn)


async def test_a_held_run_lock_holds_every_chunk(conn: Session, db, models, monkeypatch):
    job = await queued(conn, A.id)
    w = worker.JobWorker(db, CONFIG, models.gate(CONFIG))
    monkeypatch.setattr(worker, "WAIT", timedelta(0))
    async with db.held("run"):  # as another task's job or nightly
        assert not await step(w)
    # Not claimed either: a job is claimed under the lock.
    assert (await in_read(conn, jobs_store.get, job))["state"] == "queued" and not await classified(
        conn
    )
    assert await step(w)
    assert (await in_read(conn, jobs_store.get, job))["state"] == "done"


async def test_a_process_job_queued_during_a_longer_job_goes_at_its_next_chunk(
    conn: Session, db, models, monkeypatch
):
    """A re-run of the Field topic over B and C, a chunk each; prices for its model let it
    run under its ceiling."""
    monkeypatch.setitem(prices.ANTHROPIC, "test-field", (1.0, 2.0, 0.1))
    monkeypatch.setattr(worker, "CHUNK", 1)
    scope = {"topic": AGENTS.slug, "revision": 1, "scopes": ["members"], "papers": [B.id, C.id]}
    longer = await in_write(conn, jobs_store.create, "rerun", scope, 2, "queued", ceiling=1.0)
    w = worker.JobWorker(db, CONFIG, models.gate(CONFIG))
    assert await step(w)  # B
    process = await queued(conn, A.id)
    assert await step(w)  # A: the Process job first
    assert (await in_read(conn, jobs_store.get, process))["state"] == "done"
    assert (await in_read(conn, jobs_store.get, longer))["state"] == "running"
    graded = [r["paper_id"] for r in await rows(conn, "SELECT paper_id FROM verdicts ORDER BY id")]
    assert graded == [B.id] and await classified(conn) == {A.id}
    await work_out(w)  # C, then the passes it queued (none)
    assert (await in_read(conn, jobs_store.get, longer))["state"] == "done"


async def test_a_job_with_tonights_run_waits_for_a_nightly_to_finish(conn: Session, db, models):
    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "offered")
    await on_tonight(conn, job)
    w = worker.JobWorker(db, CONFIG, models.gate(CONFIG))
    assert not await step(w)
    await nightly.run(db, CONFIG, fetch=False, models=models.gate(CONFIG), with_digests=False)
    asked_before = asked(models, "classify", A.title)
    assert await step(w)
    assert (await in_read(conn, jobs_store.get, job))["state"] == "done"
    assert asked(models, "classify", A.title) == asked_before  # the night classified it


async def test_a_job_put_on_tonights_run_as_the_nightly_closes_is_released(
    conn: Session, db, models
):
    """With tonight's run is in flight, not committed, as the nightly closes its run: the
    nightly's close waits for it on the `tonight` lock, then releases the job too."""
    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "offered")
    moved, commit = asyncio.Event(), asyncio.Event()

    async def put_on_tonight() -> None:
        async with sessions.Bridge(conn).write("with tonight's run", locks=["tonight"]) as unit:
            await jobs_store.move(unit, job, jobs_store.NOT_RUN, "tonight")
            moved.set()
            await commit.wait()

    mover = asyncio.create_task(put_on_tonight())
    await asyncio.wait_for(moved.wait(), 10)
    night = asyncio.create_task(
        nightly.run(db, CONFIG, fetch=False, models=models.gate(CONFIG), with_digests=False)
    )
    await asyncio.sleep(0.5)  # the nightly runs up to its close
    commit.set()
    await mover
    await asyncio.wait_for(night, 10)
    assert await stranded(conn) == []
    assert await step(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    assert (await job_of(conn, job))["state"] == "done"


async def test_a_job_put_on_tonights_run_while_the_fires_close_waits_is_released(
    conn: Session, db, models, monkeypatch
):
    """The fire's nightly begins its closing unit, which waits on its item's row (a settling
    holds it) before the `tonight` lock; meanwhile With tonight's run puts the job there and
    commits. The close, stamped by the clock after the lock rather than as its unit began,
    comes after the job's move, so it releases the job."""
    from litwatch.jobs import scheduler  # noqa: PLC0415
    from tests.working import items_of, serving, until  # noqa: PLC0415

    monkeypatch.setattr(nightly, "locked", partial(nightly.locked, fetch=False, with_digests=False))
    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "offered")
    in_steps, go, held, let_go = (asyncio.Event() for _ in range(4))

    async def steps(*_, **__) -> None:
        in_steps.set()
        await go.wait()

    async def hold() -> None:  # the fire's item's row, as its gate's settling holds it
        async with sessions.Bridge(conn).write("hold the fire's item") as unit:
            await unit.execute("SELECT 1 FROM work_items WHERE kind = 'nightly' FOR UPDATE")
            held.set()
            await let_go.wait()

    monkeypatch.setattr(nightly, "_steps", steps)
    async with db.write("a fire's item") as unit:
        fired = scheduler.fire_key(datetime(2026, 10, 12, tzinfo=UTC))
        item = await queue.submit(unit, "nightly", fired, lasting=True)
    nightly_worker = scheduler.NightlyWorker(db, CONFIG, models.gate(CONFIG))
    async with serving(WorkGate(db, poll=0.1), {"nightly": nightly_worker}):
        holder = None
        try:
            await asyncio.wait_for(in_steps.wait(), 10)
            holder = asyncio.create_task(hold())
            await asyncio.wait_for(held.wait(), 10)
            go.set()
            await asyncio.sleep(0.5)  # its closing unit has begun, and waits on the row

            async def move() -> None:
                async with db.write("with tonight's run", locks=["tonight"]) as unit:
                    moved = await jobs_store.move(unit, job, jobs_store.NOT_RUN, "tonight")
                    assert moved is not None

            await asyncio.wait_for(move(), 10)  # the `tonight` lock included
        finally:  # a failure above lets the holder and the nightly go, rather than hang
            go.set()
            let_go.set()
            if holder is not None:
                await asyncio.wait_for(holder, 10)

        async def ended() -> bool:
            (row,) = await items_of(conn, "nightly", item.key)
            return row["state"] == "ended"

        await until(ended, "the fire's nightly to end")
    assert job in await in_read(conn, jobs_store.open_ids) and await stranded(conn) == []


async def test_a_job_put_on_tonights_run_waiting_on_the_close_waits_for_the_next_nightly(
    conn: Session, db, models, monkeypatch
):
    """With tonight's run begins while the nightly's close holds the `tonight` lock, before the
    close stamps its run, and waits on the lock: stamped by the clock after the lock rather than
    as its unit began, its job comes after that close, so it waits for the next nightly rather
    than reading as released with no chunk submitted."""
    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "offered")
    real, closing = runs.finish, asyncio.Event()

    async def slow(unit, *args, **kwargs) -> None:
        closing.set()
        await asyncio.sleep(0.5)  # the close holds the lock while the move begins, and waits
        await real(unit, *args, **kwargs)

    async def put_on_tonight() -> None:
        await asyncio.wait_for(closing.wait(), 10)
        async with db.write("with tonight's run", locks=["tonight"]) as unit:
            assert await jobs_store.move(unit, job, jobs_store.NOT_RUN, "tonight") is not None

    monkeypatch.setattr(runs, "finish", slow)
    await asyncio.gather(
        nightly.run(db, CONFIG, fetch=False, models=models.gate(CONFIG), with_digests=False),
        put_on_tonight(),
    )
    assert (await job_of(conn, job))["state"] == "tonight" and await stranded(conn) == []
    assert job not in await in_read(conn, jobs_store.open_ids)


async def test_a_nightly_that_fails_still_releases_tonights_jobs(
    conn: Session, db, models, monkeypatch
):
    async def broken(*_, **__):
        raise RuntimeError("the grading went away")

    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "offered")
    await on_tonight(conn, job)
    monkeypatch.setattr(nightly, "_steps", broken)
    with pytest.raises(RuntimeError, match="the grading went away"):
        await nightly.run(db, CONFIG, fetch=False, models=models.gate(CONFIG), with_digests=False)
    (night,) = await rows(conn, "SELECT * FROM runs WHERE kind = 'nightly'")
    assert night["finished_at"] is not None and "the grading went away" in night["error"]
    assert await stranded(conn) == []
    assert await step(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    assert (await job_of(conn, job))["state"] == "done"


async def test_a_nightly_closes_its_run_and_releases_its_jobs_together_or_not_at_all(
    conn: Session, db, models, monkeypatch
):
    """The database is lost as the nightly releases its jobs: its run stays open too, so a job
    on tonight's run is not left runnable with no chunk; it waits for a nightly that closes.
    The nightly says its run did not close (`UnclosedError`), from the error that stopped it."""

    async def lost(unit):
        raise psycopg.OperationalError("the server closed the connection unexpectedly")

    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "offered")
    await on_tonight(conn, job)
    monkeypatch.setattr(jobs_store, "release", lost)
    with pytest.raises(UnclosedError) as raised:
        await nightly.run(db, CONFIG, fetch=False, models=models.gate(CONFIG), with_digests=False)
    assert isinstance(raised.value.__cause__, psycopg.OperationalError)
    (night,) = await rows(conn, "SELECT * FROM runs WHERE kind = 'nightly'")
    assert night["finished_at"] is None
    assert await stranded(conn) == [] and (await job_of(conn, job))["state"] == "tonight"


async def test_a_fire_whose_lease_lapsed_holds_back_every_chunk_until_taken_back(
    conn: Session, db, models, monkeypatch
):
    """The fire's item runs on with its lease lapsed and its run not closed (its process died,
    its take-back not made yet): it is to run again, so the chunk waits for it."""
    monkeypatch.setattr(worker, "WAIT", timedelta(0))
    job = await queued(conn, A.id)
    await conn.execute(
        "INSERT INTO work_items (kind, key, admission, lasting, state, lease_token, lease_until) "
        "VALUES ('nightly', 'a fire', 'nightly', true, 'running', gen_random_uuid(), "
        "now() - interval '1 second')"
    )
    assert not await step(worker.JobWorker(db, SCHEDULED, models.gate(SCHEDULED)))
    assert (await job_of(conn, job))["state"] == "queued" and not await classified(conn)


async def test_a_nightly_item_taken_holds_back_every_chunk_before_its_run_opens(
    conn: Session, db, models, monkeypatch
):
    """The fire's item is running under a live lease and has not opened its run yet: the chunk
    waits, whether it sees that before the run lock or only under it. Once the run that claim
    opened has closed, the chunk goes."""
    monkeypatch.setattr(worker, "WAIT", timedelta(0))
    job = await queued(conn, A.id)
    take = (
        "INSERT INTO work_items (kind, key, admission, lasting, state, lease_token, lease_until) "
        "VALUES ('nightly', %s, 'nightly', true, 'running', gen_random_uuid(), "
        "now() + interval '1 hour')"
    )
    await conn.execute(take, ("a fire",))
    assert await in_read(conn, jobs_store.nightly_waits)
    w = worker.JobWorker(db, SCHEDULED, models.gate(SCHEDULED))
    assert not await step(w)
    await conn.execute(
        "INSERT INTO runs (kind, lease_token, finished_at) SELECT 'nightly', lease_token, now() "
        "FROM work_items WHERE kind = 'nightly'"
    )
    assert not await in_read(conn, jobs_store.nightly_waits)

    async def taken_meanwhile() -> None:  # between the look before the lock and the claim
        await conn.execute(
            "UPDATE work_items SET state = 'ended', outcome = 'done', lease_token = NULL, "
            "lease_until = NULL WHERE kind = 'nightly'"
        )
        await conn.execute(take, ("the next fire",))

    monkeypatch.setattr(w, "_evict", taken_meanwhile)
    (waited,) = await chunks(w, limit=1)
    assert isinstance(waited, Later)
    assert (await job_of(conn, job))["state"] == "queued" and not await classified(conn)


async def test_a_job_whose_chunk_raises_past_its_handling_fails_rather_than_runs_on_with_no_chunk(
    conn: Session, db, models, monkeypatch
):
    async def broken(*_, **__):
        raise RuntimeError("the end could not be read")

    job = await queued(conn, A.id)
    w = worker.JobWorker(db, CONFIG, models.gate(CONFIG))
    monkeypatch.setattr(w, "_end", broken)
    (ended,) = await chunks(w)
    assert isinstance(ended, RuntimeError)
    failed = await job_of(conn, job)
    assert (failed["state"], failed["error"]) == (
        "failed",
        "RuntimeError: the end could not be read",
    )
    assert await stranded(conn) == [] and w.open == {}


async def test_a_job_queued_again_as_it_ends_runs_again_though_closing_its_run_fails(
    conn: Session, db, models, monkeypatch
):
    """The owner asks the running Process job again, so it ends queued; then closing its run
    fails. Closing only records the run: the job's end arranged its next chunk, which runs."""
    finish, failing = runs.finish, []

    async def fails_once(unit, run_id, **kwargs):
        if not failing:
            failing.append(run_id)
            raise RuntimeError("the run record could not be written")
        return await finish(unit, run_id, **kwargs)

    async def asked_again(job, run):
        found = await chunk(job, run)
        async with db.write("the owner asks again") as unit:
            await unit.execute(
                "UPDATE jobs SET queued_at = clock_timestamp() WHERE id = %s", (job["id"],)
            )
        return found

    job = await queued(conn, A.id)
    w = worker.JobWorker(db, CONFIG, models.gate(CONFIG))
    chunk = w._chunk
    monkeypatch.setattr(w, "_chunk", asked_again)
    monkeypatch.setattr(runs, "finish", fails_once)
    assert await step(w)
    assert (await job_of(conn, job))["state"] == "queued" and failing
    assert await stranded(conn) == []
    monkeypatch.setattr(w, "_chunk", chunk)
    assert await work_out(w) == 1
    assert (await job_of(conn, job))["state"] == "done"


# -- the API --------------------------------------------------------------------------------------


@asynccontextmanager
async def api(conn: Session, config=SCHEDULED):
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, config)
    headers = {"authorization": f"Bearer {TOKEN}"}
    async with AppClient(app, base_url="http://t", headers=headers) as c:
        c.app = app
        yield c


def keys(group: list[dict]) -> list[str]:
    return [j["key"] for j in group]


async def test_jobs_are_listed_in_the_four_groups_with_the_cards_asked_for(conn: Session, models):
    offered = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "offered")
    running = await queued(conn, B.id)
    await in_write(conn, jobs_store.claim, running, afresh=True)
    failed = await in_write(conn, jobs_store.create, "process", {"papers": [C.id]}, 1, "failed")
    gone = await in_write(
        conn, jobs_store.create, "process", {"papers": ["2610.00009"]}, 1, "discarded"
    )
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


async def test_a_card_waiting_on_the_owner_fails_and_one_nothing_runs_is_left_out(
    conn: Session, models
):
    """A card awaiting its text over a week waits on the owner, as on Needs you: Failed, with
    Try again. A dismissed paper's card that never started is not listed: nothing runs it."""
    await in_write(conn, papers_store.dismiss, B.id)
    await in_write(
        conn, papers_store.dismiss, C.id
    )  # its pass claimed, its worker gone before an attempt
    for paper_id, state, queued in (
        (A.id, "awaiting_text", "8 days"),
        (B.id, "ready", "1 hour"),
        (C.id, "running", "1 hour"),
    ):
        await conn.execute(
            "INSERT INTO fulltext_passes (paper_id, paper_revision, kind, reason, priority, "
            "state, queued_at, tried_at) "
            "VALUES (%s, 1, 'deep', 'request', 0, %s, now() - %s::interval, now())",
            (paper_id, state, queued),
        )
    async with api(conn) as http:
        listed = (await http.get("/api/jobs")).json()
    (card,) = listed["failed"]
    assert (card["paper"]["id"], card["actions"]) == (A.id, ["again"])
    assert listed["running"] == []


@pytest.mark.parametrize(
    ("action", "state", "group"),
    [("run", "queued", "running"), ("tonight", "tonight", "running"), ("discard", None, None)],
)
async def test_a_job_not_run_is_run_now_put_on_tonights_run_or_discarded(
    conn: Session, models, action, state, group
):
    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "offered")
    async with api(conn) as http:
        acted = await http.post(f"/api/jobs/{job}/{action}")
        again = await http.post(f"/api/jobs/{job}/{action}")
        missing = await http.post(f"/api/jobs/{job + 99}/{action}")
    assert acted.status_code == 200 and (await in_read(conn, jobs_store.get, job))["state"] == (
        state or "discarded"
    )
    listed = acted.json()
    assert keys(listed[group]) == [f"job:{job}"] if group else not any(listed.values())
    assert (again.status_code, missing.status_code) == (409, 404)


async def test_a_failed_job_is_tried_again(conn: Session, models):
    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "failed")
    async with api(conn) as http:
        refused = await http.post(f"/api/jobs/{job}/run")
        acted = await http.post(f"/api/jobs/{job}/again")
    assert refused.status_code == 409 and refused.json()["detail"] == "the job is failed now"
    assert keys(acted.json()["running"]) == [f"job:{job}"]


async def test_a_process_job_whose_paper_waits_on_the_owner_is_not_tried_again(
    conn: Session, models
):
    """Its classification failed three runs in a row: Try again and Process would find nothing
    due, so neither is offered and both are refused; the owner's action on Needs you settles
    it. One failed run fewer, both put it back in the queue."""
    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "failed")
    await conn.execute(
        "INSERT INTO step_failures (paper_id, step, topic, paper_revision, failed_runs, last_error)"
        " VALUES (%s, 'classification', '', 1, 3, 'unusable')",
        (A.id,),
    )
    async with api(conn) as http:
        listed = (await http.get("/api/jobs")).json()
        page = (await http.get(f"/api/papers/{A.id}")).json()
        tried = await http.post(f"/api/jobs/{job}/again")
        processed = await http.post("/api/jobs/process", json={"paper_id": A.id})
    assert listed["failed"][0]["actions"] == [] and page["process"]["again"] is False
    for refused in (tried, processed):
        assert (refused.status_code, refused.json()["detail"]) == (409, jobs_api.WAITS)
    assert (await in_read(conn, jobs_store.get, job))["state"] == "failed"

    await conn.execute("UPDATE step_failures SET failed_runs = 2")
    async with api(conn) as http:
        listed = (await http.get("/api/jobs")).json()
        page = (await http.get(f"/api/papers/{A.id}")).json()
        processed = await http.post("/api/jobs/process", json={"paper_id": A.id})
    assert listed["failed"][0]["actions"] == ["again"] and page["process"]["again"] is True
    assert (processed.status_code, processed.json()["state"]) == (202, "queued")


async def test_process_fetches_the_paper_and_queues_its_job_once(
    conn: Session, db, models, monkeypatch
):
    elsewhere = paper("2610.09999", title="Elsewhere")
    monkeypatch.setattr(
        misses, "fetch_by_ids", answering(lambda ids: [elsewhere] if elsewhere.id in ids else [])
    )
    async with api(conn) as http:
        first = await http.post("/api/jobs/process", json={"paper_id": "2610.09999v2"})
        again = await http.post("/api/jobs/process", json={"paper_id": elsewhere.id})
        page = (await http.get(f"/api/papers/{elsewhere.id}")).json()
        missing = await http.post("/api/jobs/process", json={"paper_id": "2601.99999"})
    assert first.status_code == 202 and first.json()["state"] == "queued"
    assert again.json()["id"] == first.json()["id"]  # asked again, the same job
    assert (page["stored"], page["in_collection"]) == (True, False)
    assert page["process"] == {
        "id": first.json()["id"],
        "state": "queued",
        "error": None,
        "again": False,
    }
    assert missing.status_code == 404

    await work_out(worker.JobWorker(db, CONFIG, models.gate(CONFIG)))
    assert await classified(conn) == {elsewhere.id}
    async with api(conn) as http:
        page = (await http.get(f"/api/papers/{elsewhere.id}")).json()
        processed = await http.post("/api/jobs/process", json={"paper_id": elsewhere.id})
        listed = (await http.get("/api/jobs")).json()
    assert page["in_collection"] and page["process"]["state"] == "done"
    assert processed.status_code == 409
    assert keys(listed["done"]) == [f"job:{first.json()['id']}"]
    assert listed["done"][0]["cost"] > 0


async def test_process_asked_again_while_its_job_runs_only_answers_with_it(conn: Session, models):
    """A retry after a lost reply, while the job runs and has not classified the paper yet: it
    answers with the job and does not ask it again, so the job ends done, not queued."""
    job = await queued(conn, A.id)
    await in_write(conn, jobs_store.claim, job, afresh=True)
    async with api(conn) as http:
        again = await http.post("/api/jobs/process", json={"paper_id": A.id})
    assert (again.status_code, again.json()["id"], again.json()["state"]) == (202, job, "running")
    await in_write(conn, jobs_store.finish, job, "done")
    assert (await in_read(conn, jobs_store.get, job))["state"] == "done"


async def test_process_asked_twice_at_once_never_asks_its_running_job_again(
    conn: Session, models, monkeypatch
):
    """A double click: the first request queues the job and the worker claims it while the
    second looks for it. The second answers with that job and does not ask it again."""
    process_of, first = jobs_store.process_of, None

    async def queued_and_claimed():  # the first request, then the worker
        async with (
            await sessions.sibling(conn) as other,
            settling.change(other, A.id, cause=Cause.OWNER) as change,
        ):
            await jobs_store.process(change.unit, A.id)
        (job,) = await in_read(conn, jobs_store.open_ids)
        return (await in_write(conn, jobs_store.claim, job, afresh=True))["id"]

    async def overtaken(*args, **kwargs):
        nonlocal first
        found = await process_of(*args, **kwargs)
        if first is None:
            first = settling.apart(queued_and_claimed())
            await asyncio.sleep(0.3)
        return found

    monkeypatch.setattr(jobs_store, "process_of", overtaken)
    async with api(conn) as http:
        second = await http.post("/api/jobs/process", json={"paper_id": A.id})
    job = await first
    assert second.status_code == 202 and second.json()["id"] == job
    await in_write(conn, jobs_store.finish, job, "done")
    assert [r["state"] for r in await rows(conn, "SELECT state FROM jobs")] == ["done"]


async def test_a_paper_fetched_but_not_processed_can_be_processed_while_the_schedule_is_on(
    conn: Session, models
):
    """Stored by a lookup or a miss report, never processed: with the schedule on, Process
    queues its job now; with it off, the paper waits for the next run; dismissed, neither."""
    async with api(conn) as http:
        on = (await http.get(f"/api/papers/{A.id}")).json()
    async with api(conn, CONFIG) as http:
        off = (await http.get(f"/api/papers/{A.id}")).json()
    assert (on["stored"], on["in_collection"], on["process"]) == (True, False, None)
    assert (on["process_now"], off["process_now"], off["next_run"]) == (True, False, True)

    await in_write(conn, papers_store.dismiss, B.id)
    async with api(conn) as http:
        dismissed = (await http.get(f"/api/papers/{B.id}")).json()
    assert (dismissed["process_now"], dismissed["next_run"]) == (False, False)


async def test_process_holds_its_job_so_a_try_again_meanwhile_waits_and_is_refused(
    conn: Session, models, monkeypatch
):
    job = await in_write(conn, jobs_store.create, "process", {"papers": [A.id]}, 1, "failed")
    move, page = jobs_store.move, None
    async with await sessions.sibling(conn) as other:

        async def raced(c, *args):
            nonlocal page  # the Jobs page's Try again comes in while Process decides
            page = asyncio.create_task(move(staged(other), job, ("failed",), "queued"))
            await asyncio.sleep(0.3)
            return await move(c, *args)

        monkeypatch.setattr(jobs_store, "move", raced)
        processed = await in_write(conn, jobs_store.process, A.id)
        assert await page is None  # it waited, then found the job queued
    assert processed["id"] == job and processed["state"] == "queued"


async def test_the_paper_page_reads_the_job_before_the_paper(conn: Session, models, monkeypatch):
    """The job ends while the page is read: the page says it still runs, so it is read again."""
    job = await queued(conn, A.id)
    await in_write(conn, jobs_store.claim, job, afresh=True)
    read = api_reading.reading.paper

    async def ended_meanwhile(c, paper_id):
        found = await read(c, paper_id)
        # On the test's session, as another process's: the page's unit is open.
        await jobs_store.finish(staged(conn), job, "done")
        return found

    monkeypatch.setattr(api_reading.reading, "paper", ended_meanwhile)
    async with api(conn) as http:
        page = (await http.get(f"/api/papers/{A.id}")).json()
    assert page["process"]["state"] == "running"


async def test_process_without_a_taxonomy_is_refused_and_keeps_nothing(conn: Session, monkeypatch):
    elsewhere = paper("2610.09999", title="Elsewhere")
    monkeypatch.setattr(misses, "fetch_by_ids", answering(lambda ids: [elsewhere]))
    async with api(conn) as http:
        refused = await http.post("/api/jobs/process", json={"paper_id": elsewhere.id})
    assert (refused.status_code, refused.json()["detail"]) == (409, "no taxonomy yet")
    assert await rows(conn, "SELECT 1 FROM jobs UNION ALL SELECT 1 FROM papers") == []


async def test_with_the_schedule_off_process_only_fetches_the_paper_for_the_next_run(
    conn: Session, models, monkeypatch
):
    elsewhere = paper("2610.09999", title="Elsewhere")
    monkeypatch.setattr(misses, "fetch_by_ids", answering(lambda ids: [elsewhere]))
    async with api(conn, CONFIG) as http:
        fetched = await http.post("/api/jobs/process", json={"paper_id": elsewhere.id})
        page = (await http.get(f"/api/papers/{elsewhere.id}")).json()
    assert (fetched.status_code, fetched.json()) == (202, None)
    assert (page["stored"], page["next_run"], page["process"]) == (True, True, None)
    assert await rows(conn, "SELECT 1 FROM jobs") == []


async def test_process_on_a_dismissed_paper_is_refused_and_keeps_nothing(conn: Session, models):
    await in_write(conn, papers_store.dismiss, A.id)
    async with api(conn) as http:
        refused = await http.post("/api/jobs/process", json={"paper_id": A.id})
    assert refused.status_code == 409 and await rows(conn, "SELECT 1 FROM jobs") == []


async def test_process_on_a_paper_dismissed_while_its_job_waits_is_refused(conn: Session, models):
    await queued(conn, A.id)
    await in_write(conn, papers_store.dismiss, A.id)
    async with api(conn) as http:
        refused = await http.post("/api/jobs/process", json={"paper_id": A.id})
    assert (refused.status_code, refused.json()["detail"]) == (
        409,
        "the paper is dismissed or withdrawn",
    )
