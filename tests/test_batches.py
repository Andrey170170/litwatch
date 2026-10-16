"""Message Batches in the gateway (docs/specs/v1/models.md, batches): a batching job's calls go
out together and are answered as normal calls are; what a batch does not answer, a deadline
cuts or a dead run left open is handled without paying twice."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel

from litwatch import digests, fulltext, store
from litwatch.config import Config, Models, RoleConfig
from litwatch.core import FAILED_RUNS
from litwatch.core.fulltext import LEASE
from litwatch.jobs import nightly
from litwatch.jobs.context import model_run
from litwatch.jobs.lock import run_lock
from litwatch.llm import CallError, Gateway, ProviderError, Reply, Request, Stop, Usage
from litwatch.llm.anthropic import AnthropicProvider, messages_request
from litwatch.llm.batches import DRAIN, MAX_REQUESTS
from litwatch.llm.prices import anthropic_cost
from litwatch.store import papers as papers_store
from litwatch.store import runs
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store.calls import CallJournal
from tests import test_digests as dg
from tests import test_fulltext as ft
from tests import test_steps as steps
from tests.conftest import TREE
from tests.fakes import BatchingProvider, ScriptedProvider, anthropic_message
from tests.fakes_http import FakeArchive


class Answer(BaseModel):
    yes: bool
    why: str


GOOD = json.dumps({"yes": True, "why": "it says so"})
BRIEF = RoleConfig(
    provider="anthropic", model="test-brief", effort="high", fallback="brief_fallback"
)
OTHER = RoleConfig(provider="openrouter", model="lab/other")
MODELS = Models(brief=BRIEF, brief_fallback=OTHER)
START = datetime(2026, 10, 16, 9, tzinfo=UTC)  # 03:00 in Denver
DEADLINE = START + timedelta(hours=4, minutes=30)
FAST = {"quiet": 0.02, "poll": 0.01}  # the batcher's timings in tests


def ask(n: int | str) -> Request:
    return Request(system="rubric", user=f"paper {n}", output=Answer)


def respond(role: RoleConfig, request: Request) -> str | Reply:
    usage = Usage(model=role.model, via=role.provider, cost=0.001)
    if request.user == "paper refuse" and role.provider == "anthropic":
        return Reply("", Stop.REFUSED, usage, refusal="cyber")
    if request.user == "paper truncate":
        return Reply('{"yes": tr', Stop.TRUNCATED, usage)
    return GOOD


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def batching(journal=None, *, hold=False, provider=None, clock=None):
    """A gateway batching on `anthropic` until DEADLINE, by `clock`."""
    clock = clock or Clock()
    anthropic = provider or BatchingProvider("anthropic", respond, hold=hold)
    openrouter = ScriptedProvider("openrouter", respond)
    providers = {"anthropic": anthropic, "openrouter": openrouter}
    options = FAST | {"now": clock}
    gw = Gateway(MODELS, providers, journal, retry_delays=(0.0,), batch_options=options)
    gw.batch_until(DEADLINE)
    return gw, anthropic, openrouter, clock


async def until(condition, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if await condition() if asyncio.iscoroutinefunction(condition) else condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never came true")


# -- the batch path ------------------------------------------------------------------------------


async def test_a_steps_calls_go_out_together_in_one_batch():
    gw, anthropic, _, _ = batching()
    assert gw.width(4) == MAX_REQUESTS  # a step lifts its own limit while batching
    answers = await asyncio.gather(*(gw.ask("brief", ask(n)) for n in range(5)))
    assert [len(keys) for keys in anthropic.submitted] == [5]
    assert anthropic.calls == []  # no normal call
    assert {a.value.why for a in answers} == {"it says so"}
    assert {(a.usage.service_tier, a.usage.effort) for a in answers} == {("batch", "high")}
    await gw.close()


async def test_identical_calls_share_one_request_and_the_second_costs_nothing():
    gw, anthropic, _, _ = batching()
    first, second = await asyncio.gather(gw.ask("brief", ask(1)), gw.ask("brief", ask(1)))
    assert [len(keys) for keys in anthropic.submitted] == [1]
    assert sorted([first.usage.cost, second.usage.cost]) == [0.0, 0.0005]
    await gw.close()


async def test_a_gateway_that_does_not_batch_sends_normal_calls_at_the_steps_limit():
    anthropic = BatchingProvider("anthropic", respond)
    gw = Gateway(MODELS, {"anthropic": anthropic}, retry_delays=(0.0,))
    assert gw.width(4) == 4
    await gw.ask("brief", ask(1))
    assert (len(anthropic.calls), anthropic.batches) == (1, {})


async def test_what_a_batch_does_not_answer_goes_as_a_normal_call():
    gw, anthropic, _, _ = batching()
    anthropic.unanswered = lambda request: request.user == "paper 1"  # errored or expired
    await asyncio.gather(gw.ask("brief", ask(0)), gw.ask("brief", ask(1)))
    assert [len(keys) for keys in anthropic.submitted] == [2]
    assert [r.user for _, r in anthropic.calls] == ["paper 1"]
    await gw.close()


async def test_a_batch_answer_is_handled_as_a_normal_one_the_fallback_included():
    gw, anthropic, openrouter, _ = batching()
    answer = await gw.ask("brief", ask("refuse"))
    assert answer.model == OTHER.label  # the refusal went to the fallback, as a normal call
    assert [r.user for _, r in openrouter.calls] == ["paper refuse"]
    with pytest.raises(CallError) as found:
        await gw.ask("brief", ask("truncate"))
    assert found.value.kind == "truncated"
    assert anthropic.calls == []
    await gw.close()


async def test_a_batch_that_cannot_be_sent_goes_as_normal_calls(monkeypatch):
    gw, anthropic, _, _ = batching()

    async def refused(requests):
        raise ProviderError("InternalServerError")

    monkeypatch.setattr(anthropic, "submit", refused)
    await asyncio.gather(gw.ask("brief", ask(0)), gw.ask("brief", ask(1)))
    assert len(anthropic.calls) == 2
    await gw.close()


async def test_at_the_deadline_a_batch_is_cancelled_its_answers_kept_and_the_rest_sent_normally():
    gw, anthropic, _, clock = batching(hold=True)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in range(2)]
    await until(lambda: anthropic.batches)
    clock.now = DEADLINE
    await until(lambda: anthropic.cancelled == ["msgbatch_1"])
    assert not any(t.done() for t in asked)  # waiting for the cancelled batch to end
    anthropic.end("msgbatch_1", answered={gw.request_key(BRIEF, ask(0))})
    first, second = await asyncio.gather(*asked)
    assert (first.usage.service_tier, second.usage.service_tier) == ("batch", None)
    assert [r.user for _, r in anthropic.calls] == ["paper 1"]
    await gw.ask("brief", ask(2))  # past the deadline: a normal call, no batch
    assert (len(anthropic.batches), len(anthropic.calls)) == (1, 2)
    await gw.close()


async def test_calls_held_but_not_sent_at_the_deadline_go_as_normal_calls():
    gw, anthropic, _, clock = batching()
    gw.batchers["anthropic"].quiet = 0.5
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await asyncio.sleep(0.05)  # held, the batch not yet sent
    clock.now = DEADLINE
    await asked
    assert (anthropic.batches, len(anthropic.calls)) == ({}, 1)
    await gw.close()


async def test_a_batch_still_cancelling_after_the_drain_has_its_requests_sent_normally():
    gw, anthropic, _, clock = batching(hold=True)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in range(2)]
    await until(lambda: anthropic.batches)
    clock.now = DEADLINE + DRAIN
    await asyncio.gather(*asked)  # the batch never ended
    assert len(anthropic.calls) == 2
    await gw.close()


async def test_a_batch_given_up_on_is_left_open_on_its_row_for_a_later_run_to_account_for(conn):
    gw, anthropic, _, clock = batching(
        CallJournal(conn, await runs.start(conn, "nightly")), hold=True
    )
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(lambda: anthropic.batches)
    clock.now = DEADLINE + DRAIN
    await asked  # a normal call, on a row of its own
    assert [(c["batch_id"], c["answered"], c["cost"]) for c in await calls(conn)] == [
        ("msgbatch_1", False, None),
        (None, True, 0.001),
    ]
    await gw.close()
    # Days later, a run accounts for the answer the batch gave after all: both charges stand.
    await conn.execute("UPDATE inference_calls SET started_at = now() - interval '2 days'")
    later, _, _, _ = batching(
        CallJournal(conn, await runs.start(conn, "nightly")), provider=anthropic
    )
    await later.recover()
    anthropic.end("msgbatch_1", answered={gw.request_key(BRIEF, ask(0))})  # cancelled, late
    await later.settle()
    assert [(c["batch_id"], c["answered"], c["cost"]) for c in await calls(conn)] == [
        ("msgbatch_1", False, 0.0005),
        (None, True, 0.001),
    ]
    await later.close()


async def test_a_journal_that_fails_fails_the_calls_waiting_on_their_batch(conn, monkeypatch):
    journal = CallJournal(conn, await runs.start(conn, "nightly"))
    gw, _, _, _ = batching(journal)

    async def down(call_ids, batch_id):
        raise OSError("the database went away")

    monkeypatch.setattr(journal, "batched", down)
    with pytest.raises(OSError):
        await asyncio.wait_for(gw.ask("brief", ask(0)), 2)
    await gw.close()


async def test_a_batch_stays_within_the_byte_ceiling():
    gw, anthropic, _, _ = batching()
    gw.batchers["anthropic"].max_bytes = 12_000  # each request is about 5,000 bytes
    await asyncio.gather(*(gw.ask("brief", ask(f"{n} " + "x" * 3_000)) for n in range(3)))
    assert sorted(len(keys) for keys in anthropic.submitted) == [1, 2]
    await gw.close()


async def test_a_requests_size_is_counted_as_it_is_sent_its_escapes_included():
    gw, anthropic, _, _ = batching()
    gw.batchers["anthropic"].max_bytes = 12_000  # each is 3,000 backslashes: 6,000 bytes as JSON
    await asyncio.gather(*(gw.ask("brief", ask(f"{n} " + "\\" * 3_000)) for n in range(3)))
    assert [len(keys) for keys in anthropic.submitted] == [1, 1, 1]
    await gw.close()


async def test_a_fallback_on_a_provider_that_batches_is_a_normal_call():
    models = Models(
        brief=RoleConfig(provider="openrouter", model="lab/other", fallback="brief_fallback"),
        brief_fallback=RoleConfig(provider="anthropic", model="test-brief"),
    )

    def refusing(role, request):
        if role.provider == "openrouter":
            return Reply("", Stop.REFUSED, Usage(model=role.model, cost=0.001), refusal="cyber")
        return GOOD

    anthropic = BatchingProvider("anthropic", refusing)
    providers = {"anthropic": anthropic, "openrouter": ScriptedProvider("openrouter", refusing)}
    gw = Gateway(models, providers, retry_delays=(0.0,), batch_options=FAST | {"now": Clock()})
    gw.batch_until(DEADLINE)
    assert (await gw.ask("brief", ask(1))).value.yes
    assert (anthropic.batches, len(anthropic.calls)) == ({}, 1)
    await gw.close()


async def test_closing_the_gateway_sends_what_still_waits_as_normal_calls():
    gw, anthropic, _, _ = batching(hold=True)
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(lambda: anthropic.batches)
    await gw.close()
    await asked
    assert len(anthropic.calls) == 1


# -- Anthropic's batches ---------------------------------------------------------------------------


class Batches:
    """The SDK's `messages.batches`, answering every request with `message`."""

    def __init__(self, message) -> None:
        self.message, self.created = message, []

    async def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id="msgbatch_1")

    async def retrieve(self, batch_id):
        return SimpleNamespace(processing_status="ended")

    async def results(self, batch_id):
        async def lines():
            for r in self.created[-1]:
                result = SimpleNamespace(type="succeeded", message=self.message)
                yield SimpleNamespace(custom_id=r["custom_id"], result=result)
            yield SimpleNamespace(custom_id="expired", result=SimpleNamespace(type="expired"))

        return lines()


async def test_anthropic_batches_carry_the_request_key_and_are_priced_at_half():
    haiku = RoleConfig(provider="anthropic", model="claude-haiku-5-5", effort="medium")
    message = anthropic_message(GOOD)
    message.model = "claude-haiku-5-5"
    batches = Batches(message)
    provider = AnthropicProvider(client=SimpleNamespace(messages=SimpleNamespace(batches=batches)))
    request = ask(1)
    batch_id = await provider.submit([("k1", haiku, request)])
    assert batches.created == [[{"custom_id": "k1", "params": messages_request(haiku, request)}]]
    assert await provider.status(batch_id) == "ended"
    found = await provider.results(batch_id)
    assert found["expired"] is None
    usage = found["k1"].usage
    full = anthropic_cost("claude-haiku-5-5", 400, 3000, 0, 200)
    assert (usage.cost, usage.service_tier) == (pytest.approx(full / 2), "batch")


# -- the journal -----------------------------------------------------------------------------------


async def calls(conn) -> list[dict]:
    return await (
        await conn.execute(
            "SELECT run_id, batch_id, output IS NOT NULL AS answered, error, "
            "(usage->>'cost')::float AS cost FROM inference_calls ORDER BY id"
        )
    ).fetchall()


async def test_a_run_started_again_waits_on_the_batch_a_dead_run_sent(conn):
    dead = await runs.start(conn, "nightly")
    gw, anthropic, _, clock = batching(CallJournal(conn, dead), hold=True)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in range(2)]

    async def recorded():
        return all(c["batch_id"] for c in await calls(conn)) and len(await calls(conn)) == 2

    await until(recorded)
    for t in asked:  # the run dies with its batch open
        t.cancel()
    await asyncio.gather(*asked, return_exceptions=True)
    await gw.close()

    again = await runs.start(conn, "nightly")
    gw, _, _, _ = batching(CallJournal(conn, again), provider=anthropic, clock=clock)
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await asyncio.sleep(0.05)
    anthropic.end("msgbatch_1")
    answer = await asked
    assert answer.value.why == "it says so"
    assert (len(anthropic.batches), anthropic.calls) == (1, [])  # nothing paid twice
    # Both answers are on the dead run's calls; the one no call awaited has its usage only.
    assert await calls(conn) == [
        {"run_id": dead, "batch_id": "msgbatch_1", "answered": True, "error": None, "cost": 0.0005},
        {
            "run_id": dead,
            "batch_id": "msgbatch_1",
            "answered": False,
            "error": None,
            "cost": 0.0005,
        },
    ]
    dead_run = await runs.get(conn, dead)  # its totals take them, as its close would have
    assert (float(dead_run["cost"]), dead_run["counts"]["calls"]) == (
        0.001,
        {"brief": {"calls": 2, "cost": 0.001}},
    )
    await gw.close()


async def test_a_batch_a_dead_run_left_is_accounted_for_though_nothing_asks_again(conn):
    dead = await runs.start(conn, "nightly")
    gw, anthropic, _, clock = batching(CallJournal(conn, dead), hold=True)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in range(2)]

    async def recorded():
        return all(c["batch_id"] for c in await calls(conn)) and len(await calls(conn)) == 2

    await until(recorded)
    for t in asked:
        t.cancel()
    await asyncio.gather(*asked, return_exceptions=True)
    await gw.close()

    again = await runs.start(conn, "nightly")
    gw, _, _, _ = batching(CallJournal(conn, again), provider=anthropic, clock=clock)
    await gw.recover()
    settling = asyncio.create_task(gw.settle())  # as a nightly does before it ends
    await asyncio.sleep(0.05)
    assert not settling.done()
    anthropic.end("msgbatch_1")
    await settling
    assert [(c["answered"], c["cost"]) for c in await calls(conn)] == [(False, 0.0005)] * 2
    await gw.close()


async def test_a_run_past_the_deadline_cancels_a_dead_runs_batch_and_keeps_its_answers(conn):
    dead = await runs.start(conn, "nightly")
    gw, anthropic, _, clock = batching(CallJournal(conn, dead), hold=True)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in range(2)]

    async def recorded():
        return all(c["batch_id"] for c in await calls(conn)) and len(await calls(conn)) == 2

    await until(recorded)
    for t in asked:
        t.cancel()
    await asyncio.gather(*asked, return_exceptions=True)
    await gw.close()

    # Started after the deadline, the run sends no batch; the dead run's is cancelled at once.
    clock.now = DEADLINE + timedelta(hours=1)
    again = await runs.start(conn, "nightly")
    gw, _, _, _ = batching(CallJournal(conn, again), provider=anthropic, clock=clock)
    gw.batch_until(clock.now)
    await gw.recover()
    await until(lambda: anthropic.cancelled == ["msgbatch_1"])
    first = asyncio.create_task(gw.ask("brief", ask(0)))
    await asyncio.sleep(0.05)
    anthropic.end("msgbatch_1", answered={gw.request_key(BRIEF, ask(0))})
    assert (await first).usage.service_tier == "batch"  # the answer it had is kept
    await gw.ask("brief", ask(1))  # cancelled before it was answered: a normal call
    assert (len(anthropic.batches), len(anthropic.calls)) == (1, 1)
    assert anthropic.cancelled == ["msgbatch_1"]  # an ended batch is not cancelled again
    await gw.close()


async def test_a_run_past_the_drain_reads_a_dead_runs_ended_batch_before_giving_up(conn):
    dead = await runs.start(conn, "nightly")
    gw, anthropic, _, clock = batching(CallJournal(conn, dead))
    await asyncio.gather(*(gw.ask("brief", ask(n)) for n in range(2)))
    await conn.execute("UPDATE inference_calls SET output = NULL, usage = '{}'")  # died unread
    await gw.close()

    clock.now = DEADLINE + DRAIN + timedelta(hours=1)
    again = await runs.start(conn, "nightly")
    gw, _, _, _ = batching(CallJournal(conn, again), provider=anthropic, clock=clock)
    gw.batch_until(clock.now - DRAIN - timedelta(minutes=1))  # the drain passed already
    await gw.recover()
    await gw.settle()
    assert [c["cost"] for c in await calls(conn)] == [0.0005, 0.0005]
    assert (await gw.ask("brief", ask(0))).usage.service_tier == "batch"
    assert anthropic.calls == []  # nothing paid twice
    await gw.close()


async def test_a_call_whose_batch_did_not_answer_it_is_settled_and_asked_again(conn):
    dead = await runs.start(conn, "nightly")
    gw, anthropic, _, clock = batching(CallJournal(conn, dead), hold=True)
    anthropic.unanswered = lambda request: True
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(lambda: anthropic.batches)
    await asyncio.sleep(0.05)
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()
    anthropic.end("msgbatch_1")
    anthropic.hold, anthropic.unanswered = False, lambda request: False

    again = await runs.start(conn, "nightly")
    gw, _, _, _ = batching(CallJournal(conn, again), provider=anthropic, clock=clock)
    await gw.ask("brief", ask(0))
    found = await calls(conn)
    assert [(c["run_id"], c["batch_id"], c["error"], c["answered"]) for c in found] == [
        (dead, "msgbatch_1", "provider: the batch did not answer it", False),
        (again, None, None, True),
    ]
    assert (len(anthropic.batches), len(anthropic.calls)) == (1, 1)  # a normal call, no batch
    await gw.close()


# -- the nightly run -------------------------------------------------------------------------------


@asynccontextmanager
async def another_schema(conn):
    """A second empty, migrated schema beside `conn`'s, as the `conn` fixture makes one."""
    schema = "test_" + uuid4().hex
    async with await store.connect(conn.test_dsn) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            async with await store.connect(conn.test_dsn, schema) as c:
                await store.migrate(c)
                c.test_dsn, c.test_schema = conn.test_dsn, schema
                yield c
        finally:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def factory(respond, anthropic: BatchingProvider):
    """A nightly's gateways over `anthropic`, which batches, and a scripted OpenRouter."""

    def gateway(config: Config, journal) -> Gateway:
        providers = {"anthropic": anthropic, "openrouter": ScriptedProvider("openrouter", respond)}
        return Gateway(config.models, providers, journal, retry_delays=(0.0,), batch_options=FAST)

    return gateway


def tonight(late: bool = False) -> tuple[datetime, datetime]:
    """A night begun an hour ago whose deadline is an hour away, or `late`: passed a minute ago."""
    now = datetime.now(UTC)
    return now - timedelta(hours=1), now + (-timedelta(minutes=1) if late else timedelta(hours=1))


async def records(conn) -> dict[str, list]:
    """What a night's steps recorded, without ids, times and the runs that wrote them."""
    found = {}
    for table in ("classifications", "memberships", "analyses", "verdicts", "fulltext_passes"):
        rows = await (await conn.execute(f"SELECT * FROM {table}")).fetchall()
        found[table] = sorted(
            repr(sorted((k, v) for k, v in r.items() if k not in ("id", "run_id") and
                        not k.endswith("_at")))
            for r in rows
        )  # fmt: skip
    return found


@pytest.mark.parametrize("night", ["batched", "late"])
async def test_a_batched_night_records_what_an_unbatched_one_does(conn, night):
    """V5, and V7's late start: a nightly run started after its deadline does not batch."""

    async def one_night(c, batching: str | None) -> tuple[dict, BatchingProvider]:
        await taxonomy_store.save(c, TREE)
        await papers_store.upsert(c, [steps.A, steps.B])
        models = steps.Scripted()
        anthropic = BatchingProvider("anthropic", models.respond)
        when = tonight(late=batching == "late") if batching else None
        gateway = factory(models.respond, anthropic)
        await nightly.run(c, steps.CONFIG, fetch=False, gateway=gateway, night=when)
        return await records(c), anthropic

    expected, plain = await one_night(conn, None)
    async with another_schema(conn) as other:
        found, anthropic = await one_night(other, night)
    assert found == expected and expected["verdicts"]
    if night == "batched":
        assert anthropic.calls == []
        assert [len(keys) for keys in anthropic.submitted][:1] == [2]  # both papers' analyses
    else:
        assert anthropic.batches == {} and len(anthropic.calls) == len(plain.calls)


async def test_a_run_whose_deadline_passes_before_its_steps_waits_for_the_nights_passes(
    conn, monkeypatch
):
    """A run started in the window whose deadline passed by its steps does not batch, and its
    digests still wait for the passes queued since the night's first run."""
    night = tonight(late=True)
    first = await runs.start(conn, "nightly")
    waited = []

    async def wait_for_texts(conn, config, run, since, deadline, limit=None):
        waited.append(since)

    monkeypatch.setattr(nightly, "wait_for_texts", wait_for_texts)
    anthropic = BatchingProvider("anthropic", respond)
    gateway = factory(respond, anthropic)
    await nightly.run(conn, steps.CONFIG, fetch=False, gateway=gateway, night=night)
    started = (await runs.get(conn, first))["started_at"]
    assert waited == [started] and anthropic.batches == {}


async def passing(conn):
    """The full-text tests' paper, classified and analysed, graded central: its Brief queued."""
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [ft.P])
    await conn.execute(
        "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
        "primary_class, model) VALUES (%s, 1, 'x', 'interpretability', 'm')",
        (ft.P.id,),
    )
    await ft.analysed(conn, "solid")  # nothing left for the night's other steps
    await ft.graded(conn, "central")


async def test_a_nightly_started_again_takes_back_its_passes_and_pays_for_nothing_twice(
    conn, monkeypatch
):
    """V6: a nightly killed with its pass's call in an open batch; the next one takes the pass
    back at once and has it answered from that batch."""
    await passing(conn)
    fake = ft.ready(FakeArchive())
    monkeypatch.setattr(fulltext, "archive_for", lambda config: ft.archive(fake))
    writer = ft.Writer()
    anthropic = BatchingProvider("anthropic", writer.respond, hold=True)
    night = tonight()
    killed = asyncio.create_task(
        nightly.run(
            conn, ft.CONFIG, fetch=False, gateway=factory(writer.respond, anthropic), night=night
        )
    )
    await until(lambda: anthropic.batches)
    killed.cancel()
    await asyncio.gather(killed, return_exceptions=True)
    (row,) = await ft.passes(conn)
    assert row["state"] == "running" and row["lease_until"] > datetime.now(UTC) + LEASE
    # Started again late: less of the lease is left than a worker's, and it is still taken back.
    await conn.execute(
        "UPDATE fulltext_passes SET tried_at = now() - interval '5 hours', "
        "lease_until = now() + interval '10 minutes'"
    )

    anthropic.end("msgbatch_1")
    run_id = await nightly.run(
        conn, ft.CONFIG, fetch=False, gateway=factory(writer.respond, anthropic), night=night
    )
    counts = (await runs.get(conn, run_id))["counts"]
    assert (counts["fulltext_taken_back"], counts["briefs_written"]) == (1, 1)
    assert (len(anthropic.batches), anthropic.calls) == (1, [])  # submitted once, by the first
    assert (await ft.passes(conn))[0]["state"] == "done"


async def test_a_worker_waits_on_a_dead_nightlys_batch_rather_than_paying_again(conn, monkeypatch):
    """A nightly dies with its pass's call in a batch and is not started again; once the lease
    runs out the service's worker takes the pass and is answered from that batch."""
    await passing(conn)
    fake = ft.ready(FakeArchive())
    monkeypatch.setattr(fulltext, "archive_for", lambda config: ft.archive(fake))
    writer = ft.Writer()
    anthropic = BatchingProvider("anthropic", writer.respond, hold=True)
    gateway = factory(writer.respond, anthropic)
    killed = asyncio.create_task(
        nightly.run(conn, ft.CONFIG, fetch=False, gateway=gateway, night=tonight())
    )
    await until(lambda: anthropic.batches)
    killed.cancel()
    await asyncio.gather(killed, return_exceptions=True)
    anthropic.end("msgbatch_1")
    await conn.execute("UPDATE fulltext_passes SET lease_until = now() - interval '1 second'")

    found = await fulltext.work(conn, ft.CONFIG, ft.archive(fake), gateway=gateway)
    assert found == {"briefs_written": 1}
    assert (len(anthropic.batches), anthropic.calls) == (1, [])


@asynccontextmanager
async def nightly_running(conn):
    """Another session holds the run lock, as a nightly does."""
    async with await store.connect(conn.test_dsn, conn.test_schema) as other, run_lock(other):
        yield


async def batching_work(conn, writer, fake, anthropic):
    """`fulltext.work` as a batching nightly runs it."""
    gateway = factory(writer.respond, anthropic)
    async with model_run(conn, ft.CONFIG, "nightly", gateway=gateway) as run:
        run.gateway.batch_until(tonight()[1])
        try:
            return await fulltext.work(conn, ft.CONFIG, ft.archive(fake), run=run)
        finally:
            await run.gateway.close()


async def test_during_a_nightly_its_automatic_passes_are_its_own_batched(conn):
    """V8: the service's worker leaves an automatic pass to the nightly, which batches it."""
    await passing(conn)
    fake, writer = ft.ready(FakeArchive()), ft.Writer()
    anthropic = BatchingProvider("anthropic", writer.respond)
    async with nightly_running(conn):
        assert await ft.work(conn, fake, writer) == {}  # the worker
        assert (await ft.passes(conn))[0]["state"] == "awaiting_text"
        assert await batching_work(conn, writer, fake, anthropic) == {"briefs_written": 1}
    assert (len(anthropic.submitted), anthropic.calls) == (1, [])


async def test_a_worker_leaves_the_nightly_its_passes_from_the_claim_after_it_starts(
    conn, monkeypatch
):
    """V8: a worker already working when the nightly starts takes no automatic pass after."""
    await passing(conn)
    other = ft.P.model_copy(update={"id": "2610.00099"})  # its Brief queued after P's
    await papers_store.upsert(conn, [other])
    cols = ", ".join(c for c in (await ft.passes(conn))[0] if c not in ("id", "paper_id"))
    await conn.execute(
        f"INSERT INTO fulltext_passes (paper_id, {cols}) SELECT %s, {cols} "
        "FROM fulltext_passes WHERE paper_id = %s",
        (other.id, ft.P.id),
    )
    starts = iter([False, True, True])  # the nightly takes the lock after the first claim

    async def held(conn):
        return next(starts)

    monkeypatch.setattr(fulltext, "held_elsewhere", held)
    monkeypatch.setattr(fulltext, "BATCH", 1)
    assert await ft.work(conn, ft.ready(FakeArchive()), ft.Writer()) == {"briefs_written": 1}


@pytest.mark.parametrize("action", ["now", "pdf"])
async def test_during_a_nightly_the_owners_pass_is_the_workers_at_once(conn, monkeypatch, action):
    """V8: "run it now" and "Use the PDF" make the pass the owner's: the batching nightly leaves
    it, and the service's worker runs it with a normal call while the nightly holds the lock."""
    monkeypatch.setattr("litwatch.archive.text.pdf_to_markdown", lambda pdf: "From the PDF. " * 40)
    await passing(conn)
    writer = ft.Writer()
    await ft.work(conn, FakeArchive(status="failed"), writer)
    await conn.execute("UPDATE fulltext_passes SET failed_runs = %s", (FAILED_RUNS,))
    (row,) = await ft.passes(conn)
    _, http = ft.client(conn)
    async with http:
        assert (await http.post(f"/api/settings/fulltext/{row['id']}/{action}")).status_code == 204
    fake = ft.ready(FakeArchive())
    anthropic = BatchingProvider("anthropic", writer.respond)
    async with nightly_running(conn):
        assert await batching_work(conn, writer, fake, anthropic) == {}
        assert await ft.work(conn, fake, writer) == {"briefs_written": 1}
    assert anthropic.batches == {}


async def test_topics_digests_written_together_are_those_written_one_after_another(conn):
    """V11: a Monday's digests through batches, topics concurrently, equal those written in
    turn: a failed digest written again before the day's, the weeklies after the dailies."""

    async def monday(c, together: bool) -> tuple[list, BatchingProvider | None]:
        await taxonomy_store.save(c, TREE)
        await papers_store.upsert(c, [dg.P, dg.Q, dg.R, dg.S])
        friday = dg.MON - timedelta(days=3)
        await dg.focus(c, dg.P, "central")
        await dg.listed(c, friday, dg.P)
        failing = dg.Writer()
        failing.failures = [ProviderError("down")]
        await dg.day(c, failing, friday)  # the Focus topic's Friday digest failed
        await dg.focus(c, dg.Q, "related")
        await dg.area(c, dg.R, "central")  # must read
        await dg.listed(c, dg.MON, dg.Q, dg.R)
        w, anthropic = dg.Writer(), None
        gateway = w.gateway
        if together:
            anthropic = BatchingProvider("anthropic", w.respond)
            gateway = factory(w.respond, anthropic)
        tree = await taxonomy_store.load(c)
        async with model_run(c, Config(), "nightly", gateway=gateway) as run:
            if together:
                run.gateway.batch_until(tonight()[1])
                await digests.nightly(c, run, tree, dg.MON)
                await run.gateway.close()
            else:
                await digests.retry_failed(c, run, tree)
                await digests.write_day(c, run, tree, dg.MON)
                await digests.write_week(c, run, tree, dg.MON - timedelta(days=7))
        rows = await (
            await c.execute("SELECT topic, period, day, state, text FROM digests ORDER BY 1, 2, 3")
        ).fetchall()
        found = [(*r.values(), await dg.shown(c, r["topic"], r["period"], r["day"])) for r in rows]
        return found, anthropic

    expected, _ = await monday(conn, together=False)
    async with another_schema(conn) as other:
        found, anthropic = await monday(other, together=True)
    assert found == expected
    assert sum(r[3] == "written" for r in found) >= 3
    assert anthropic.calls == [] and len(anthropic.batches) <= 3  # rounds, not one per digest
