"""The process's one Model gate (docs/plans/a2-work-gate.md, P4; V4): built once by `serve`
and shared by the items it runs, which give their calls a deadline through the work context;
batches shared across items with one provider and deadline, each settled once on its calls'
rows whatever happens to the items that wait on it, and a batch a killed item left recovered
once as `serve` starts, credited to its original call and item."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from litwatch.api import app as app_module
from litwatch.config import Config, Models, RoleConfig
from litwatch.core import Cause, Fit, Verdict
from litwatch.gates.work import CommandCutOffError, inline
from litwatch.jobs.context import model_run
from litwatch.llm import ModelGate, Request
from litwatch.llm.context import spending
from litwatch.llm.openai import OpenAIProvider
from litwatch.store import papers as papers_store
from litwatch.store import runs, verdicts
from litwatch.store.calls import CallJournal
from tests import briefs as ft
from tests import settling
from tests.conftest import FAITH, TREE, paper
from tests.fakes import (
    FAST,
    BatchingProvider,
    OpenAIBatches,
    ScriptedProvider,
    SdkClient,
    openai_response,
)
from tests.fakes_http import FakeArchive
from tests.sessions import Session, in_read, in_write
from tests.working import QUIET, served, until

BRIEF = RoleConfig(provider="anthropic", model="test-brief")
SOL = RoleConfig(provider="openai", model="gpt-6.2-sol", effort="high")
GOOD = json.dumps({"yes": True})


class Answer(BaseModel):
    yes: bool


def ask(n: int) -> Request:
    return Request(system="rubric", user=f"paper {n}", output=Answer)


def respond(_role: RoleConfig, _request: Request) -> str:
    return GOOD


def tonight() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def gate_over(db, provider, role: RoleConfig = BRIEF, quiet: float | None = None) -> ModelGate:
    """A process's Model gate over `provider`, serving `brief` as `role`, its journal on `db`;
    a batch goes out once no call has joined it for `quiet` seconds (FAST's by default)."""
    options = FAST | ({} if quiet is None else {"quiet": quiet})
    providers = {provider.name: provider}
    journal = CallJournal(db)
    return ModelGate(
        Models(brief=role), providers, journal, retry_delays=(0.0,), batch_options=options
    )


async def run_cost(conn: Session, run_id: int) -> float:
    """The run's cost, as its tally recorded it."""
    found = await in_read(conn, runs.get, run_id)
    assert found is not None
    return float(found["cost"])


def asked_in_an_item(db, gate: ModelGate, name: str, n: int, deadline: datetime | None):
    """A command item `name` (`gates.work.inline`) whose run asks `brief` about paper `n`
    through `gate`, under `deadline`: its task."""

    async def work():
        async with model_run(db, Config(), "nightly", models=gate) as run:
            with spending(deadline=deadline):
                return await run.gateway.ask("brief", ask(n))

    return asyncio.create_task(inline(db, name, work))


async def rows(conn: Session, query: str, *params) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def journalled(conn: Session) -> list[dict]:
    """Each call's run, item, batch and charge, with the item's key."""
    return await rows(
        conn,
        "SELECT c.id, c.run_id, c.work_id, c.batch_id, c.output IS NOT NULL AS answered, "
        "(c.usage->>'cost')::float AS cost, w.key FROM inference_calls c "
        "LEFT JOIN work_items w ON w.id = c.work_id ORDER BY c.id",
    )


def batched(conn: Session, n: int):
    async def check() -> bool:
        found = await journalled(conn)
        return len(found) == n and all(c["batch_id"] for c in found)

    return check


# -- one gate per process ------------------------------------------------------------------------


class Held(ScriptedProvider):
    """Answers once `release` is set, counting the calls in flight at once (`most`)."""

    def __init__(self, name: str, respond) -> None:
        super().__init__(name, respond)
        self.release, self.flying, self.most = asyncio.Event(), 0, 0

    async def complete(self, role, request):
        self.flying += 1
        self.most = max(self.most, self.flying)
        try:
            await self.release.wait()
            return await super().complete(role, request)
        finally:
            self.flying -= 1


async def wanted(conn: Session, *papers) -> None:
    """Each paper classified under Interpretability and graded central on the Focus topic: its
    Brief pass queued, each in a slot of its own."""
    await settling.save_tree(conn, TREE)
    await in_write(conn, papers_store.upsert, list(papers))
    for p in papers:
        await conn.execute(
            "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
            "primary_class, model) VALUES (%s, 1, 'x', 'interpretability', 'm')",
            (p.id,),
        )
        found = Verdict(
            paper_id=p.id, topic=FAITH.slug, topic_version="v", fit=Fit.CENTRAL, reason="r",
            opinions={},
        )  # fmt: skip
        await in_write(conn, verdicts.add, found)
        await settling.settle(conn, p.id, cause=Cause.VERDICT)


async def test_serve_builds_one_gate_whose_limits_its_concurrent_items_share(
    conn: Session, db, monkeypatch
):
    """Two text passes run at once, each its own item and run; their calls go through the one
    gate `serve` built, which lets one call at a time to the provider here."""
    q = paper("2610.00002", title="Monitors under pressure", abstract="Monitors.", version=1)
    await wanted(conn, ft.P, q)
    fake = FakeArchive()
    for p in (ft.P, q):
        fake.texts[p.id] = (1, ft.BODY)
    held, built = Held("anthropic", ft.Writer().respond), []

    def model_gate(database, config):
        built.append(database)
        journal = CallJournal(database)
        return ModelGate(
            config.models, {"anthropic": held}, journal, retry_delays=(0.0,), normal_calls=1
        )

    monkeypatch.setattr(app_module, "model_gate", model_gate)
    config = ft.CONFIG.model_copy(update={"schedule": QUIET})

    async def both_open() -> bool:
        found = await rows(
            conn, "SELECT 1 FROM runs WHERE kind = 'fulltext' AND finished_at IS NULL"
        )
        return len(found) == 2 and held.flying == 1

    async def both_done() -> bool:
        found = await rows(conn, "SELECT state FROM fulltext_passes WHERE kind <> 'deep'")
        return [r["state"] for r in found] == ["done", "done"]

    async with served(db, config, archive=ft.archive(fake)):
        await until(both_open, "both passes' runs to open, one call in flight")
        await asyncio.sleep(0.1)
        assert held.flying == 1  # the second waits at the gate's limit, not at a gate of its own
        held.release.set()
        await until(both_done, "both Briefs to be written")
    assert built == [db] and held.most == 1
    found = await rows(
        conn,
        "SELECT c.run_id, c.work_id, r.work_id AS run_work, w.kind FROM inference_calls c "
        "JOIN runs r ON r.id = c.run_id JOIN work_items w ON w.id = c.work_id",
    )
    assert len({c["run_id"] for c in found}) == len({c["work_id"] for c in found}) == 2
    assert all(c["work_id"] == c["run_work"] and c["kind"] == "fulltext" for c in found)


# -- batches across items ------------------------------------------------------------------------


async def test_items_with_one_deadline_share_a_batch_whose_calls_settle_after_a_lease_is_lost(
    conn: Session, db
):
    """Two items' calls under one deadline go in one batch; the first item's lease is lost
    while it waits (another process took its item), and the batch still settles both calls'
    rows, each on its own run and item, and answers the second."""
    anthropic = BatchingProvider("anthropic", respond, hold=True)
    gate = gate_over(db, anthropic, quiet=0.5)
    deadline = tonight()
    first = asked_in_an_item(db, gate, "first", 1, deadline)
    second = asked_in_an_item(db, gate, "second", 2, deadline)
    await until(batched(conn, 2), "both calls in a batch")
    assert [len(keys) for keys in anthropic.submitted] == [2]  # one batch, the two items'
    await conn.execute(
        "UPDATE work_items SET lease_token = gen_random_uuid() WHERE key LIKE 'first:%%'"
    )
    anthropic.end("msgbatch_1")
    assert (await asyncio.wait_for(second, 5)).value.yes
    with pytest.raises(CommandCutOffError):  # its run's close is fenced on the lease it lost
        await asyncio.wait_for(first, 5)
    found = await journalled(conn)
    assert sorted(c["key"].split(":")[0] for c in found) == ["first", "second"]
    assert [c["cost"] for c in found] == [0.0005, 0.0005] and anthropic.calls == []
    assert len({c["run_id"] for c in found}) == 2
    assert next(c for c in found if c["key"].startswith("second"))["answered"]
    await gate.close()


async def test_a_killed_items_batch_is_recovered_once_as_serve_starts_and_credited_to_its_call(
    conn: Session, db, monkeypatch
):
    """The item is killed with its call in an open batch; the next `serve` takes the batch over
    as it starts, reads it once it ends, and credits its charge to that call, on the dead item's
    run and item, whose cost the run's tally then takes."""
    anthropic = BatchingProvider("anthropic", respond, hold=True)
    dead = gate_over(db, anthropic)  # the killed process's
    killed = asked_in_an_item(db, dead, "nightly", 1, tonight())
    await until(batched(conn, 1), "the call's batch on its row")
    killed.cancel()
    await asyncio.gather(killed, return_exceptions=True)
    await dead.close()
    (left,) = await journalled(conn)
    assert left["cost"] is None and left["work_id"] is not None
    anthropic.end("msgbatch_1")

    results, read = anthropic.results, []

    async def counted(batch_id):
        read.append(batch_id)
        return await results(batch_id)

    monkeypatch.setattr(anthropic, "results", counted)
    gate = gate_over(db, anthropic)
    recover, recovered = gate.recover, []

    async def counting(deadline):
        recovered.append(deadline)
        return await recover(deadline)

    monkeypatch.setattr(gate, "recover", counting)

    async def credited() -> bool:
        return (await journalled(conn))[0]["cost"] == 0.0005

    async with served(db, Config(), models=gate):
        await until(credited, "the dead item's batch to be read")
    (found,) = await journalled(conn)
    assert (found["run_id"], found["work_id"]) == (left["run_id"], left["work_id"])
    assert len(recovered) == 1 and read == ["msgbatch_1"] and anthropic.calls == []
    assert await run_cost(conn, left["run_id"]) == 0.0005
    await gate.close()


# -- the OpenAI adapter ----------------------------------------------------------------------------


async def test_openais_normal_and_batched_charges_are_journalled_on_the_items_own_run(
    conn: Session, db
):
    client = OpenAIBatches()
    client.responses = SimpleNamespace(create=SdkClient(openai_response(GOOD)).responses.create)
    gate = gate_over(db, OpenAIProvider(client=client), SOL)

    async def made() -> bool:
        return bool(client.made)

    async def work():
        async with model_run(db, Config(), "nightly", models=gate) as run:
            normal = await run.gateway.ask("brief", ask(1))
            with spending(deadline=tonight()):
                waiting = asyncio.create_task(run.gateway.ask("brief", ask(2)))
            await until(made, "the batch to be made")
            client.end("batch_1")
            return run.run_id, normal, await waiting

    run_id, normal, batch = await inline(db, "openai", work)
    assert (normal.usage.service_tier, batch.usage.service_tier) == ("default", "batch")
    found = await journalled(conn)
    assert [c["cost"] for c in found] == [
        pytest.approx(normal.usage.cost),
        pytest.approx(batch.usage.cost),
    ]
    assert batch.usage.cost and normal.usage.cost == pytest.approx(2 * batch.usage.cost)
    assert {c["run_id"] for c in found} == {run_id}
    assert {c["key"].split(":")[0] for c in found} == {"openai"}  # the command's item
    total = normal.usage.cost + batch.usage.cost
    assert await run_cost(conn, run_id) == pytest.approx(total, abs=5e-5)  # kept to 4 places
    await gate.close()
