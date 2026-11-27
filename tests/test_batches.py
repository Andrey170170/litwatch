"""Message Batches in the gateway (docs/specs/v1/models.md, batches): a batching job's calls go
out together and are answered as normal calls are; what a batch does not answer, a deadline
cuts or a dead run left open is handled without paying twice."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx2
import openai
import psycopg
import pytest
from pydantic import BaseModel

from litwatch import digests, fulltext
from litwatch.archive import PdfText
from litwatch.config import Config, Models, RoleConfig
from litwatch.core import FAILED_RUNS
from litwatch.core.budget import CallStart
from litwatch.core.fulltext import LEASE
from litwatch.core.work import ItemState, WorkItem
from litwatch.gates.context import Work, working
from litwatch.gates.database import LOCKS, DatabaseGate
from litwatch.jobs import nightly
from litwatch.jobs.context import model_run
from litwatch.llm import (
    CallError,
    ModelGate,
    Provider,
    ProviderError,
    Reply,
    Request,
    Stop,
    Usage,
)
from litwatch.llm.anthropic import AnthropicProvider, messages_request
from litwatch.llm.batches import DRAIN, MAX_REQUESTS, Batcher
from litwatch.llm.context import spending
from litwatch.llm.gateway import NO_DEADLINE
from litwatch.llm.openai import OpenAIProvider, responses_request
from litwatch.llm.prices import anthropic_cost
from litwatch.store import calls as calls_store
from litwatch.store import fulltext as fulltext_store
from litwatch.store import papers as papers_store
from litwatch.store import runs
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store.calls import CallJournal
from tests import briefs as ft
from tests import digesting as dg
from tests import scripted as steps
from tests import sessions, settling
from tests.conftest import TREE, gate_on, lease_lapses, started
from tests.fakes import (
    FAST,
    BatchingProvider,
    OpenAIBatches,
    ScriptedProvider,
    SdkClient,
    anthropic_message,
    door,
    openai_body,
    openai_response,
    status_error,
)
from tests.fakes_http import FakeArchive
from tests.sessions import Session, in_read, in_write


class Answer(BaseModel):
    yes: bool
    why: str


GOOD = json.dumps({"yes": True, "why": "it says so"})
OTHER = RoleConfig(provider="openrouter", model="lab/other")
BRIEF = RoleConfig(provider="anthropic", model="test-brief", effort="high", fallbacks=(OTHER,))
MODELS = Models(brief=BRIEF)
START = datetime(2026, 10, 16, 9, tzinfo=UTC)  # 03:00 in Denver
DEADLINE = START + timedelta(hours=4, minutes=30)


class Staged(list):
    """A `staged` that records the references it was told."""

    async def __call__(self, reference: str | None) -> None:
        self.append(reference)


def ask(n: int | str) -> Request:
    return Request(system="rubric", user=f"paper {n}", output=Answer)


def respond(role: RoleConfig, request: Request) -> str | Reply | ProviderError:
    usage = Usage(model=role.model, via=role.provider, cost=0.001)
    if request.user == "paper account" and role.provider == "anthropic":
        return ProviderError("authentication_error in batch", account=True)
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


class Silent:
    """A run's journal that keeps nothing (`llm.gateway.Journal`): the batch path without a
    database, its batches on no rows."""

    run_id: int | None = 0
    observes: bool = True

    def __init__(self) -> None:
        self.started = 0

    async def cached(self, key: str) -> None:
        return None

    async def start(self, call: CallStart) -> int:
        self.started += 1
        return self.started

    async def record(self, call_id: int, usage: Usage) -> None:
        return None

    async def accept(self, call_id: int, output: dict) -> None:
        return None

    async def reject(self, call_id, error, reply=None, *, account=None, usage=None) -> None:
        return None

    async def refused(self, key: str, paper_id: str | None) -> bool:
        return False

    async def in_batch(self, key: str, work_id: int | None, owned: list[int]) -> None:
        return None


class Tonight:
    """A run's door to a Model gate of its own whose calls run under `deadline` (DEADLINE, as a
    nightly's item sets it, `llm.context.spending`; None: a worker's calls, which only attach to
    a batch), the gate's batches journalled on the run's journal when it has one (a run's
    `CallJournal` serves as the gate's too: the gate's operations name no run)."""

    def __init__(self, gate: ModelGate, journal=None, deadline: datetime | None = DEADLINE) -> None:
        self.gate, self.door, self.deadline = gate, gate.on(journal or Silent()), deadline

    def __getattr__(self, name: str):
        return getattr(self.door, name)

    @property
    def debug(self) -> bool:
        return self.door.debug

    @debug.setter
    def debug(self, on: bool) -> None:
        self.door.debug = on

    @property
    def batchers(self) -> dict[str, Batcher | None]:
        until = self.deadline or NO_DEADLINE
        return {name: self.gate.batcher(name, until) for name in self.gate.providers}

    async def ask(self, *args, **kwargs):
        with spending(deadline=self.deadline):
            return await self.door.ask(*args, **kwargs)

    def width(self, limit: int) -> int:
        with spending(deadline=self.deadline):
            return self.door.width(limit)

    async def recover(self) -> int:
        """As serve starts: the batches left open, watched until the deadline (now without)."""
        return await self.gate.recover(self.deadline or self.gate.now())

    async def settle(self) -> None:
        await self.gate.settle(self.deadline or NO_DEADLINE)

    async def close(self) -> None:
        await self.gate.close()


def batching(journal=None, *, hold=False, provider=None, clock=None, deadline=DEADLINE):
    """A run's door batching on `anthropic` until `deadline`, by `clock`."""
    clock = clock or Clock()
    anthropic = provider or BatchingProvider("anthropic", respond, hold=hold)
    openrouter = ScriptedProvider("openrouter", respond)
    providers = {"anthropic": anthropic, "openrouter": openrouter}
    options = FAST | {"now": clock}
    gate = ModelGate(MODELS, providers, journal, retry_delays=(0.0,), batch_options=options)
    return Tonight(gate, journal, deadline), anthropic, openrouter, clock


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
    gw = door(MODELS, {"anthropic": anthropic}, retry_delays=(0.0,))
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
    assert answer.model == OTHER.label  # the refusal went to the fallback, at once
    assert [r.user for _, r in openrouter.calls] == ["paper refuse"]
    with pytest.raises(CallError) as found:
        await gw.ask("brief", ask("truncate"))
    assert found.value.kind == "truncated"
    assert anthropic.calls == []
    await gw.close()


async def test_a_batch_refused_before_it_is_staged_goes_as_normal_calls(monkeypatch, caplog):
    gw, anthropic, _, _ = batching()

    async def refused(requests, staged):
        raise ProviderError("BadRequestError: too large")

    monkeypatch.setattr(anthropic, "submit", refused)
    await asyncio.gather(gw.ask("brief", ask(0)), gw.ask("brief", ask(1)))
    assert len(anthropic.calls) == 2
    assert "was not made; its calls go as normal calls" in caplog.text
    assert "BadRequestError: too large" in caplog.text  # the refusal, not a mismatched fake
    await gw.close()


@pytest.mark.parametrize("debug", [False, True])
async def test_an_account_error_in_a_batch_is_never_asked_again_nor_down_the_list(
    conn: Session, db, debug
):
    gw, anthropic, openrouter, _ = batching(Together(db, await started(conn, "nightly"), 2))
    gw.debug = debug  # which keeps the text of a reply it could not use, and here there is none
    # Bounded: a call that fails before the barrier would leave the other waiting there.
    asked = await asyncio.wait_for(
        asyncio.gather(
            gw.ask("brief", ask("account")), gw.ask("brief", ask(0)), return_exceptions=True
        ),
        5,
    )
    assert isinstance(asked[0], CallError) and asked[0].kind == "account"
    assert asked[1].value.yes
    assert (anthropic.calls, openrouter.calls) == ([], [])
    assert unordered((c["batch_id"], c["error"]) for c in await calls(conn)) == unordered(
        [
            ("msgbatch_1", "account: authentication_error in batch"),
            ("msgbatch_1", None),
        ]
    )
    await gw.close()


async def test_a_batch_the_account_kept_from_being_sent_fails_its_calls_on_the_account(
    monkeypatch,
):
    gw, anthropic, openrouter, _ = batching()

    async def unauthorized(requests, staged):
        raise ProviderError("AuthenticationError: invalid x-api-key", account=True)

    monkeypatch.setattr(anthropic, "submit", unauthorized)
    asked = await asyncio.gather(
        gw.ask("brief", ask(0)), gw.ask("brief", ask(1)), return_exceptions=True
    )
    assert [(type(a), a.kind) for a in asked] == [(CallError, "account")] * 2
    assert (anthropic.calls, openrouter.calls) == ([], [])  # never asked again
    await gw.close()


async def test_a_batch_the_account_can_no_longer_read_fails_its_calls_and_stays_open(
    conn: Session, db, monkeypatch
):
    gw, anthropic, _, _ = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(lambda: anthropic.batches)
    reads = []

    async def unauthorized(batch_id):
        reads.append(batch_id)
        raise ProviderError("AuthenticationError: invalid x-api-key", account=True)

    monkeypatch.setattr(anthropic, "status", unauthorized)
    with pytest.raises(CallError) as found:
        await asked
    assert found.value.kind == "account" and reads == ["msgbatch_1"] and anthropic.calls == []
    # The batch stays open on its row, for a later run to account for; nothing else was sent.
    assert [(c["batch_id"], c["error"]) for c in await calls(conn)] == [("msgbatch_1", None)]
    # The account works again and the call is asked again: the batch's answer settles its own
    # row, charged once there, and nothing is left open for a later run to charge again.
    monkeypatch.undo()
    anthropic.end("msgbatch_1")
    await gw.ask("brief", ask(0))
    assert [(c["batch_id"], c["answered"], c["error"]) for c in await calls(conn)] == [
        ("msgbatch_1", True, None)
    ]
    assert await CallJournal(db, await started(conn, "nightly")).open_batches() == []
    await gw.close()


async def test_a_batch_given_up_on_for_the_account_settles_its_row_with_the_answer_it_gave(
    conn: Session, db, monkeypatch
):
    gw, anthropic, _, _ = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    asked = asyncio.create_task(gw.ask("brief", ask("account")))
    await until(lambda: anthropic.batches)

    async def unauthorized(batch_id):
        raise ProviderError("AuthenticationError: invalid x-api-key", account=True)

    monkeypatch.setattr(anthropic, "status", unauthorized)
    with pytest.raises(CallError):
        await asked
    # Readable again, the batch answered the request with an account error: on the batch's row,
    # settled for good; the calls sent nothing else.
    monkeypatch.undo()
    anthropic.end("msgbatch_1")
    with pytest.raises(CallError) as found:
        await gw.ask("brief", ask("account"))
    assert found.value.kind == "account"
    assert [(c["batch_id"], c["error"]) for c in await calls(conn)] == [
        ("msgbatch_1", "account: authentication_error in batch")
    ]
    assert await CallJournal(db, await started(conn, "nightly")).open_batches() == []
    await gw.close()


async def test_a_call_attached_to_a_batch_read_meanwhile_is_answered_past_the_drain():
    clock = Clock()
    clock.now = DEADLINE + DRAIN
    other_read = asyncio.Event()
    reads = {"A": 0}

    class Provider:
        async def status(self, batch_id):
            if batch_id == "B":
                await other_read.wait()
                return "ended"
            reads["A"] += 1
            raise ProviderError("AuthenticationError: invalid x-api-key", account=True)

        async def results(self, batch_id):
            return {}

    batcher = Batcher(Provider(), None, DEADLINE, now=clock, **FAST)
    batcher.watch(1, "A")
    batcher.watch(2, "B")
    await until(lambda: reads["A"])  # A given up on for the account, B still being read
    attached = asyncio.create_task(batcher.attach(1, "A", "key", 1, until=DEADLINE))
    await asyncio.sleep(0.05)
    other_read.set()
    reply = await asyncio.wait_for(attached, 1)
    assert isinstance(reply, ProviderError) and reply.account
    await batcher.close()


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


async def test_a_batch_given_up_on_is_left_open_on_its_row_for_a_later_run_to_account_for(
    conn: Session, db
):
    gw, anthropic, _, clock = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
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
    later, _, _, _ = batching(CallJournal(db, await started(conn, "nightly")), provider=anthropic)
    await later.recover()
    anthropic.end("msgbatch_1", answered={gw.request_key(BRIEF, ask(0))})  # cancelled, late
    await later.settle()
    assert [(c["batch_id"], c["answered"], c["cost"]) for c in await calls(conn)] == [
        ("msgbatch_1", False, 0.0005),
        (None, True, 0.001),
    ]
    await later.close()


async def test_a_request_a_batch_refused_for_the_account_fails_the_provider(conn: Session, db):
    gw, _, _, _ = batching(CallJournal(db, await started(conn, "nightly")))
    with pytest.raises(CallError) as found:
        await gw.ask("brief", ask("account"))  # the batch's only request, awaited
    assert found.value.kind == "account"
    seen = (await in_read(conn, calls_store.observations))["anthropic"]
    assert (seen["account_error"], seen["replied_at"]) == (
        "account: authentication_error in batch",
        None,
    )
    await gw.close()


@pytest.mark.parametrize("refused", ["the read", "the request"])
async def test_a_dead_runs_batch_refused_for_the_account_fails_the_provider_without_a_call(
    conn: Session, db, monkeypatch, refused
):
    gw, anthropic, _, _ = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    asked = asyncio.create_task(gw.ask("brief", ask("account" if refused == "the request" else 0)))
    await until(batch_recorded(conn))
    asked.cancel()  # the run dies with its batch open, recorded on its call's row
    with pytest.raises(asyncio.CancelledError):
        await asked
    await gw.close()
    # The next run reads the batch, which no call of its own waits on.
    later, _, _, _ = batching(CallJournal(db, await started(conn, "nightly")), provider=anthropic)
    if refused == "the read":

        async def unauthorized(batch_id):
            raise ProviderError("AuthenticationError: invalid x-api-key", account=True)

        monkeypatch.setattr(anthropic, "status", unauthorized)
        said = "account: AuthenticationError: invalid x-api-key"
    else:
        anthropic.end("msgbatch_1")
        said = "account: authentication_error in batch"
    await later.recover()
    await later.settle()
    # The provider is failed for its account; a request the batch refused is settled unbilled,
    # and a batch that could not be read stays open on its row, its charge unknown.
    seen = (await in_read(conn, calls_store.observations))["anthropic"]
    assert (seen["account_error"], seen["replied_at"]) == (said, None)
    charged = 0.0 if refused == "the request" else None
    assert [(c["batch_id"], c["cost"]) for c in await calls(conn)] == [("msgbatch_1", charged)]
    await later.close()


async def test_a_journal_that_fails_fails_the_calls_waiting_on_their_batch(
    conn: Session, db, monkeypatch
):
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, _, _, _ = batching(journal)

    async def down(call_ids, batch_id, provider):
        raise OSError("the database went away")

    monkeypatch.setattr(journal, "batched", down)
    with pytest.raises(OSError):
        await asyncio.wait_for(gw.ask("brief", ask(0)), 2)
    await gw.close()


async def test_a_journal_that_fails_on_one_batch_fails_only_that_batchs_calls(
    conn: Session, db, monkeypatch
):
    """Batch B is still being made as A's settlement fails: A's call fails, and B, owned by a
    task of its own, is still recorded, read and settled, its call answered from it."""
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, anthropic, _, _ = batching(journal, hold=True)
    submit, settle, making, made = anthropic.submit, journal.settle_batch, asyncio.Event(), []

    async def held_second(requests, staged=None):
        made.append(requests)
        if len(made) == 2:  # the second batch waits inside its creation
            await making.wait()
        return await submit(requests, staged)

    async def fails_on_the_first(batch_id, answers, **rest):
        if batch_id == "msgbatch_1":
            raise OSError("the database went away")
        await settle(batch_id, answers, **rest)

    monkeypatch.setattr(anthropic, "submit", held_second)
    monkeypatch.setattr(journal, "settle_batch", fails_on_the_first)
    first = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: len(anthropic.batches) == 1)
    second = asyncio.create_task(gw.ask("brief", ask(2)))
    await until(lambda: len(made) == 2)
    anthropic.end("msgbatch_1")
    with pytest.raises(OSError):
        await asyncio.wait_for(first, 2)
    making.set()
    await until(lambda: len(anthropic.batches) == 2)
    anthropic.end("msgbatch_2")
    assert (await asyncio.wait_for(second, 2)).usage.service_tier == "batch"
    assert [(c["batch_id"], c["answered"], c["cost"]) for c in await calls(conn)] == [
        ("msgbatch_1", False, None),  # left open, for a later run to account for
        ("msgbatch_2", True, 0.0005),
    ]
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


async def test_a_batched_refusal_goes_down_the_list_in_the_same_run_batched_too():
    sonnet = RoleConfig(provider="anthropic", model="test-sonnet")
    models = Models(brief=RoleConfig(provider="anthropic", model="test-opus", fallbacks=(sonnet,)))

    def refusing(role, request):
        if role.model == "test-opus":
            return Reply("", Stop.REFUSED, Usage(model=role.model, cost=0.001), refusal="bio")
        return GOOD

    anthropic = BatchingProvider("anthropic", refusing)
    options = FAST | {"now": Clock()}
    gate = ModelGate(models, {"anthropic": anthropic}, retry_delays=(0.0,), batch_options=options)
    gw = Tonight(gate)
    answer = await gw.ask("brief", ask(1))
    assert answer.value.yes and answer.model == sonnet.label
    # Two batches, the second asking the next model; no normal call: the job decides.
    assert [m.model for m, _ in anthropic.batched] == ["test-opus", "test-sonnet"]
    assert (len(anthropic.batches), anthropic.calls) == (2, [])
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


async def test_anthropic_batch_requests_the_account_kept_unanswered_are_account_errors():
    provider = AnthropicProvider(client=SimpleNamespace(messages=SimpleNamespace(batches=None)))

    def errored(kind: str):
        error = SimpleNamespace(error=SimpleNamespace(type=kind))
        return SimpleNamespace(type="errored", error=error)

    lines = [
        SimpleNamespace(custom_id=kind, result=errored(kind))
        for kind in ("authentication_error", "billing_error", "permission_error", "api_error")
    ]

    async def results(batch_id):
        async def each():
            for line in lines:
                yield line

        return each()

    provider.client.messages.batches = SimpleNamespace(results=results)
    found = await provider.results("msgbatch_1")
    assert found["api_error"] is None  # asked again as a normal call
    for kind in ("authentication_error", "billing_error", "permission_error"):
        assert isinstance(found[kind], ProviderError) and found[kind].account


async def test_anthropic_batches_carry_the_request_key_and_are_priced_at_half():
    haiku = RoleConfig(provider="anthropic", model="claude-haiku-5-5", effort="medium")
    message = anthropic_message(GOOD)
    message.model = "claude-haiku-5-5"
    batches = Batches(message)
    provider = AnthropicProvider(client=SimpleNamespace(messages=SimpleNamespace(batches=batches)))
    request = ask(1)
    staged = Staged()
    batch_id = await provider.submit([("k1", haiku, request)], staged)
    assert staged == [None]  # just before it was created, with no reference
    assert batches.created == [[{"custom_id": "k1", "params": messages_request(haiku, request)}]]
    assert await provider.status(batch_id) == "ended"
    found = await provider.results(batch_id)
    assert found["expired"] is None
    reply = found["k1"]
    assert isinstance(reply, Reply)
    usage = reply.usage
    full = anthropic_cost("claude-haiku-5-5", 400, 3000, 0, 200)
    assert (usage.cost, usage.service_tier) == (pytest.approx(full / 2), "batch")


# -- the journal -----------------------------------------------------------------------------------


async def calls(conn: Session) -> list[dict]:
    return await (
        await conn.execute(
            "SELECT run_id, batch_id, output IS NOT NULL AS answered, error, "
            "(usage->>'cost')::float AS cost FROM inference_calls ORDER BY id"
        )
    ).fetchall()


def batched(conn: Session, n: int):
    """Whether `n` calls' rows all name their batch, read once: an empty read is not all of
    them named (the rows of a batch open together, as it is sent)."""

    async def check() -> bool:
        found = await calls(conn)
        return len(found) == n and all(c["batch_id"] for c in found)

    return check


def unordered(rows) -> list:
    """Rows in an order of their own: a run's concurrent calls start in units of their own, so
    their ids follow no order of the calls'."""
    return sorted(rows, key=repr)


class Together(CallJournal):
    """A journal whose first `n` calls wait for each other once they have looked for a batch to
    attach to, so they join one batch however slow the database: on a slow runner the reads
    alone can outlast the batcher's quiet, and the calls go in batches of their own."""

    def __init__(self, db, run_id: int, n: int) -> None:
        super().__init__(db, run_id)
        self._together, self._left = asyncio.Barrier(n), n

    async def in_batch(self, key: str, work_id: int | None, owned: list[int] | None = None):
        row = await super().in_batch(key, work_id, owned)
        if self._left > 0:
            self._left -= 1
            await self._together.wait()
        return row


async def test_a_run_started_again_waits_on_the_batch_a_dead_run_sent(conn: Session, db):
    dead = await started(conn, "nightly")
    gw, anthropic, _, clock = batching(Together(db, dead, 2), hold=True)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in range(2)]

    await until(batched(conn, 2))
    for t in asked:  # the run dies with its batch open
        t.cancel()
    await asyncio.gather(*asked, return_exceptions=True)
    await gw.close()

    again = await started(conn, "nightly")
    gw, _, _, _ = batching(CallJournal(db, again), provider=anthropic, clock=clock)
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await asyncio.sleep(0.05)
    anthropic.end("msgbatch_1")
    answer = await asked
    assert answer.value.why == "it says so"
    assert (len(anthropic.batches), anthropic.calls) == (1, [])  # nothing paid twice
    # Both answers are on the dead run's calls; the one no call awaited has its usage only.
    assert unordered(await calls(conn)) == unordered(
        [
            {
                "run_id": dead,
                "batch_id": "msgbatch_1",
                "answered": True,
                "error": None,
                "cost": 0.0005,
            },
            {
                "run_id": dead,
                "batch_id": "msgbatch_1",
                "answered": False,
                "error": None,
                "cost": 0.0005,
            },
        ]
    )
    dead_run = await in_read(conn, runs.get, dead)  # its totals take them, as its close would have
    assert (float(dead_run["cost"]), dead_run["counts"]["calls"]) == (
        0.001,
        {"brief": {"calls": 2, "cost": 0.001}},
    )
    await gw.close()


async def test_a_batch_a_dead_run_left_is_accounted_for_though_nothing_asks_again(
    conn: Session, db
):
    dead = await started(conn, "nightly")
    gw, anthropic, _, clock = batching(Together(db, dead, 2), hold=True)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in range(2)]

    await until(batched(conn, 2))
    for t in asked:
        t.cancel()
    await asyncio.gather(*asked, return_exceptions=True)
    await gw.close()

    again = await started(conn, "nightly")
    gw, _, _, _ = batching(CallJournal(db, again), provider=anthropic, clock=clock)
    await gw.recover()
    settling = asyncio.create_task(gw.settle())  # as a nightly does before it ends
    await asyncio.sleep(0.05)
    assert not settling.done()
    anthropic.end("msgbatch_1")
    await settling
    assert [(c["answered"], c["cost"]) for c in await calls(conn)] == [(False, 0.0005)] * 2
    await gw.close()


async def test_an_account_error_a_dead_runs_batch_gave_is_journalled_as_one(conn: Session, db):
    dead = await started(conn, "nightly")
    gw, anthropic, _, clock = batching(Together(db, dead, 2), hold=True)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in ("account", 0)]

    await until(batched(conn, 2))
    for t in asked:
        t.cancel()
    await asyncio.gather(*asked, return_exceptions=True)
    await gw.close()

    gw, _, _, _ = batching(
        CallJournal(db, await started(conn, "nightly")), provider=anthropic, clock=clock
    )
    await gw.recover()
    anthropic.end("msgbatch_1")
    await gw.settle()
    assert unordered((c["error"], c["cost"]) for c in await calls(conn)) == unordered(
        [
            ("account: authentication_error in batch", 0.0),  # refused: not billed
            (None, 0.0005),
        ]
    )
    await gw.close()


async def test_a_run_past_the_deadline_cancels_a_dead_runs_batch_and_keeps_its_answers(
    conn: Session, db
):
    dead = await started(conn, "nightly")
    gw, anthropic, _, clock = batching(Together(db, dead, 2), hold=True)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in range(2)]

    await until(batched(conn, 2))
    for t in asked:
        t.cancel()
    await asyncio.gather(*asked, return_exceptions=True)
    await gw.close()

    # Started after the deadline, the run sends no batch; the dead run's is cancelled at once.
    clock.now = DEADLINE + timedelta(hours=1)
    again = await started(conn, "nightly")
    gw, _, _, _ = batching(
        CallJournal(db, again), provider=anthropic, clock=clock, deadline=clock.now
    )
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


async def test_a_run_past_the_drain_reads_a_dead_runs_ended_batch_before_giving_up(
    conn: Session, db
):
    dead = await started(conn, "nightly")
    gw, anthropic, _, clock = batching(CallJournal(db, dead))
    await asyncio.gather(*(gw.ask("brief", ask(n)) for n in range(2)))
    await conn.execute(
        "UPDATE inference_calls SET output = NULL, usage = usage - 'cost', responded_at = NULL"
    )  # unread
    await gw.close()

    clock.now = DEADLINE + DRAIN + timedelta(hours=1)
    again = await started(conn, "nightly")
    passed = clock.now - DRAIN - timedelta(minutes=1)  # the drain passed already
    gw, _, _, _ = batching(CallJournal(db, again), provider=anthropic, clock=clock, deadline=passed)
    await gw.recover()
    await gw.settle()
    assert [c["cost"] for c in await calls(conn)] == [0.0005, 0.0005]
    assert (await gw.ask("brief", ask(0))).usage.service_tier == "batch"
    assert anthropic.calls == []  # nothing paid twice
    await gw.close()


async def dead_batches(conn: Session, db, n: int) -> tuple[BatchingProvider, Clock]:
    """A dead run's `n` batches of one call each, ended, their answers never read."""
    gw, anthropic, _, clock = batching(CallJournal(db, await started(conn, "nightly")))
    for i in range(n):
        await gw.ask("brief", ask(i))
    await conn.execute(
        "UPDATE inference_calls SET output = NULL, usage = usage - 'cost', responded_at = NULL"
    )
    await gw.close()
    return anthropic, clock


async def test_batches_attached_while_another_is_read_are_read_before_any_is_given_up(
    conn: Session, db, monkeypatch
):
    anthropic, clock = await dead_batches(conn, db, 2)
    status = anthropic.status

    async def slow(batch_id):  # the second call attaches while the first batch is read
        await asyncio.sleep(0.05)
        return await status(batch_id)

    monkeypatch.setattr(anthropic, "status", slow)
    # As the service's worker attaches: no deadline of its own.
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, _, _, _ = batching(journal, provider=anthropic, clock=clock, deadline=None)
    answers = await asyncio.gather(gw.ask("brief", ask(0)), gw.ask("brief", ask(1)))
    assert {a.usage.service_tier for a in answers} == {"batch"} and anthropic.calls == []
    await gw.close()


async def test_a_provider_operation_that_hangs_gives_way_to_normal_calls(monkeypatch):
    gw, anthropic, _, clock = batching(hold=True)
    gw.batchers["anthropic"].operation = 0.05
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(lambda: anthropic.batches)

    async def hangs(batch_id):
        await asyncio.sleep(3600)

    monkeypatch.setattr(anthropic, "status", hangs)
    clock.now = DEADLINE + DRAIN
    await asyncio.wait_for(asked, 2)
    assert len(anthropic.calls) == 1
    await gw.close()


async def test_batches_whose_reads_hang_are_given_up_together_not_one_after_another(
    conn: Session, db, monkeypatch
):
    anthropic, _ = await dead_batches(conn, db, 8)

    async def hangs(batch_id):
        await asyncio.sleep(3600)

    monkeypatch.setattr(anthropic, "status", hangs)
    # As the service's worker attaches: no deadline of its own.
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, _, _, _ = batching(journal, provider=anthropic, deadline=None)
    gw.batchers["anthropic"].operation = 0.2  # one after another would take 1.6 s
    try:
        asked = asyncio.gather(*(gw.ask("brief", ask(i)) for i in range(8)))
        await asyncio.wait_for(asked, 1.0)
        assert len(anthropic.calls) == 8
    finally:  # whatever happened, the hung reads are cancelled, so teardown does not wait
        await gw.close()


async def test_a_batch_its_provider_no_longer_has_is_settled_not_waited_on(conn: Session, db):
    """Deleted, or the key is now another workspace's: it never answers, so it is not waited on
    until the deadline's drain, nor recovered by every run until its results expire."""
    anthropic, _ = await dead_batches(conn, db, 2)
    del anthropic.batches["msgbatch_1"]
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, _, _, _ = batching(journal, provider=anthropic)
    await gw.recover()
    await asyncio.wait_for(gw.settle(), 2)  # the deadline is hours away
    assert await journal.open_batches() == []
    errors = {c["batch_id"]: c["error"] for c in await calls(conn)}
    assert errors["msgbatch_1"] == "provider: the batch is gone" and errors["msgbatch_2"] is None
    await gw.close()


async def test_a_dead_runs_batch_is_recovered_by_its_provider_whatever_roles_use_now(
    conn: Session, db
):
    anthropic, clock = await dead_batches(conn, db, 1)
    moved = Models(brief=OTHER)  # brief moved to OpenRouter since the run died
    providers = {"anthropic": anthropic, "openrouter": ScriptedProvider("openrouter", respond)}
    journal = CallJournal(db, await started(conn, "nightly"))
    options = FAST | {"now": clock}
    gate = ModelGate(moved, providers, journal, retry_delays=(0.0,), batch_options=options)
    gw = Tonight(gate, journal)
    await gw.recover()
    await gw.settle()
    assert [c["cost"] for c in await calls(conn)] == [0.0005]
    await gw.close()


async def test_a_call_whose_batch_did_not_answer_it_is_settled_and_asked_again(conn: Session, db):
    dead = await started(conn, "nightly")
    gw, anthropic, _, clock = batching(CallJournal(db, dead), hold=True)
    anthropic.unanswered = lambda request: True
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(lambda: anthropic.batches)
    await asyncio.sleep(0.05)
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()
    anthropic.end("msgbatch_1")
    anthropic.hold, anthropic.unanswered = False, lambda request: False

    again = await started(conn, "nightly")
    gw, _, _, _ = batching(CallJournal(db, again), provider=anthropic, clock=clock)
    await gw.ask("brief", ask(0))
    found = await calls(conn)
    assert [(c["run_id"], c["batch_id"], c["error"], c["answered"]) for c in found] == [
        (dead, "msgbatch_1", "provider: the batch did not answer it", False),
        (again, None, None, True),
    ]
    assert (len(anthropic.batches), len(anthropic.calls)) == (1, 1)  # a normal call, no batch
    await gw.close()


# -- the nightly run -------------------------------------------------------------------------------


def factory(respond, anthropic: BatchingProvider, db: DatabaseGate, config: Config) -> ModelGate:
    """A process's Model gate over `anthropic`, which batches, and a scripted OpenRouter, its
    own journal on `db`."""
    providers = {"anthropic": anthropic, "openrouter": ScriptedProvider("openrouter", respond)}
    journal = CallJournal(db)
    return ModelGate(config.models, providers, journal, retry_delays=(0.0,), batch_options=FAST)


def tonight(late: bool = False) -> tuple[datetime, datetime]:
    """A night begun an hour ago whose deadline is an hour away, or `late`: passed a minute ago."""
    now = datetime.now(UTC)
    return now - timedelta(hours=1), now + (-timedelta(minutes=1) if late else timedelta(hours=1))


async def records(conn: Session) -> dict[str, list]:
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
async def test_a_batched_night_records_what_an_unbatched_one_does(conn: Session, night):
    """V5, and V7's late start: a nightly run started after its deadline does not batch."""

    async def one_night(c: Session, batching: str | None) -> tuple[dict, BatchingProvider]:
        await settling.save_tree(c, TREE)
        await in_write(c, papers_store.upsert, [steps.A, steps.B])
        models = steps.Scripted()
        anthropic = BatchingProvider("anthropic", models.respond)
        when = tonight(late=batching == "late") if batching else None
        async with gate_on(c) as db, factory(models.respond, anthropic, db, steps.CONFIG) as gate:
            await nightly.run(db, steps.CONFIG, fetch=False, models=gate, night=when)
        return await records(c), anthropic

    expected, plain = await one_night(conn, None)
    async with sessions.schema(conn.test_dsn) as other:  # a second one
        found, anthropic = await one_night(other, night)
    assert found == expected and expected["verdicts"]
    if night == "batched":
        assert anthropic.calls == []
        assert [len(keys) for keys in anthropic.submitted][:1] == [2]  # both papers' analyses
    else:
        assert anthropic.batches == {} and len(anthropic.calls) == len(plain.calls)


async def test_a_run_whose_deadline_passes_before_its_steps_waits_for_the_nights_passes(
    conn: Session, db, monkeypatch
):
    """A run started in the window whose deadline passed by its steps does not batch, and its
    digests still wait for the passes queued since the night's first run."""
    night = tonight(late=True)
    first = await started(conn, "nightly")
    waited = []

    async def wait_for_texts(db, config, run, since, deadline, limit=None):
        waited.append((since, deadline - time.monotonic()))

    monkeypatch.setattr(nightly, "wait_for_texts", wait_for_texts)
    anthropic = BatchingProvider("anthropic", respond)
    async with factory(respond, anthropic, db, steps.CONFIG) as gate:
        await nightly.run(db, steps.CONFIG, fetch=False, models=gate, night=night)
    began = (await in_read(conn, runs.get, first))["started_at"]
    ((since, left),) = waited
    assert since == began and anthropic.batches == {}
    assert left <= 0  # the night's deadline has passed: no wait begins again


async def test_leases_are_taken_back_after_a_taxonomy_edit_in_flight(conn: Session, db):
    """Under the taxonomy lock, shared, as every settle: never in another order than a taxonomy
    save dropping the same passes."""
    async with await sessions.connect(conn.test_dsn, conn.test_schema) as other:
        async with other.transaction():
            await other.execute("SELECT pg_advisory_xact_lock(%s)", (LOCKS["taxonomy"].key,))
            taking = asyncio.create_task(take_back(db))
            await asyncio.sleep(0.3)
            assert not taking.done()
        assert await asyncio.wait_for(taking, 2) == 0


async def take_back(db) -> int:
    async with db.write("take back leases", taxonomy="shared") as unit:
        return await fulltext_store.take_back(unit, LEASE)


def batch_recorded(conn: Session):
    """Whether a call's row has its batch's id, as a restart needs to attach to the batch: a
    run killed between submitting and recording pays again, as the spec allows."""

    async def check() -> bool:
        found = "SELECT 1 FROM inference_calls WHERE batch_id IS NOT NULL"
        return await (await conn.execute(found)).fetchone() is not None

    return check


async def passing(conn: Session):
    """The full-text tests' paper, classified and analysed, graded central: its Brief queued."""
    await settling.save_tree(conn, TREE)
    await in_write(conn, papers_store.upsert, [ft.P])
    await conn.execute(
        "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
        "primary_class, model) VALUES (%s, 1, 'x', 'interpretability', 'm')",
        (ft.P.id,),
    )
    await ft.analysed(conn, "solid")  # nothing left for the night's other steps
    await ft.graded(conn, "central")


async def test_a_nightly_started_again_takes_back_its_passes_and_pays_for_nothing_twice(
    conn: Session, db, monkeypatch
):
    """V6: a nightly killed with its pass's call in an open batch; the next one takes the pass
    back at once and has it answered from that batch."""
    await passing(conn)
    fake = ft.ready(FakeArchive())
    monkeypatch.setattr(fulltext, "archive_for", lambda config: ft.archive(fake))
    writer = ft.Writer()
    anthropic = BatchingProvider("anthropic", writer.respond, hold=True)
    night = tonight()
    dead = factory(writer.respond, anthropic, db, ft.CONFIG)  # the killed process's
    killed = asyncio.create_task(nightly.run(db, ft.CONFIG, fetch=False, models=dead, night=night))
    await until(batch_recorded(conn))  # killed only once a restart can attach
    killed.cancel()
    await asyncio.gather(killed, return_exceptions=True)
    await dead.close()
    (row,) = await ft.passes(conn)
    assert row["state"] == "running" and row["lease_until"] > datetime.now(UTC) + LEASE
    # Started again late: less of the lease is left than a worker's, and it is still taken back.
    await conn.execute(
        "UPDATE fulltext_passes SET tried_at = now() - interval '5 hours', "
        "lease_until = now() + interval '10 minutes'"
    )

    anthropic.end("msgbatch_1")
    async with factory(writer.respond, anthropic, db, ft.CONFIG) as gate:
        run_id = await nightly.run(db, ft.CONFIG, fetch=False, models=gate, night=night)
    counts = (await in_read(conn, runs.get, run_id))["counts"]
    assert (counts["fulltext_taken_back"], counts["briefs_written"]) == (1, 1)
    assert (len(anthropic.batches), anthropic.calls) == (1, [])  # submitted once, by the first
    assert (await ft.passes(conn))[0]["state"] == "done"


@pytest.mark.parametrize("still_running", [False, True])
async def test_a_worker_answers_from_a_dead_nightlys_batch_rather_than_paying_again(
    conn: Session, db, monkeypatch, still_running
):
    """A nightly dies with its pass's call in a batch and is not started again; once the lease
    runs out the service's worker takes the pass and is answered from that batch if it ended,
    without waiting on it if not."""
    await passing(conn)
    fake = ft.ready(FakeArchive())
    monkeypatch.setattr(fulltext, "archive_for", lambda config: ft.archive(fake))
    writer = ft.Writer()
    anthropic = BatchingProvider("anthropic", writer.respond, hold=True)
    dead = factory(writer.respond, anthropic, db, ft.CONFIG)  # the killed process's
    killed = asyncio.create_task(
        nightly.run(db, ft.CONFIG, fetch=False, models=dead, night=tonight())
    )
    await until(batch_recorded(conn))  # killed only once a restart can attach
    killed.cancel()
    await asyncio.gather(killed, return_exceptions=True)
    await dead.close()
    await conn.execute("UPDATE fulltext_passes SET lease_until = now() - interval '1 second'")
    gate = factory(writer.respond, anthropic, db, ft.CONFIG)  # the service's
    worked = fulltext.work(db, ft.CONFIG, ft.archive(fake), models=gate)
    if still_running:  # cancelled and given up at once: a normal call, well within the lease
        found = await asyncio.wait_for(worked, 5)
        assert (anthropic.cancelled, len(anthropic.calls)) == (["msgbatch_1"], 1)
    else:
        anthropic.end("msgbatch_1")
        found = await worked
        assert anthropic.calls == []
    assert found == {"briefs_written": 1} and len(anthropic.batches) == 1
    await gate.close()


@asynccontextmanager
async def nightly_running(conn: Session):
    """Another session holds the run lock, as a nightly does."""
    async with gate_on(conn) as other, other.held("run"):
        yield


async def batching_work(conn: Session, writer, fake, anthropic, until=None):
    """`fulltext.work` as a nightly runs it, batching until `until` (the night's deadline)."""
    async with (
        gate_on(conn) as db,
        factory(writer.respond, anthropic, db, ft.CONFIG) as gate,
        model_run(db, "nightly", models=gate) as run,
    ):
        with spending(deadline=until or tonight()[1]):
            return await fulltext.work(db, ft.CONFIG, ft.archive(fake), run=run)


async def test_during_a_nightly_its_automatic_passes_are_its_own_batched(conn: Session):
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
    conn: Session, monkeypatch
):
    """V8: a worker already working when the nightly starts takes no automatic pass after."""
    await passing(conn)
    other = ft.P.model_copy(update={"id": "2610.00099"})  # its Brief queued after P's
    await in_write(conn, papers_store.upsert, [other])
    cols = ", ".join(c for c in (await ft.passes(conn))[0] if c not in ("id", "paper_id"))
    await conn.execute(
        f"INSERT INTO fulltext_passes (paper_id, {cols}) SELECT %s, {cols} "
        "FROM fulltext_passes WHERE paper_id = %s",
        (other.id, ft.P.id),
    )
    starts = iter([False, True, True])  # the nightly takes the lock after the first claim

    async def held(self, name, *, mine=None):
        return next(starts)

    monkeypatch.setattr(DatabaseGate, "held_elsewhere", held)
    monkeypatch.setattr(fulltext, "BATCH", 1)
    assert await ft.work(conn, ft.ready(FakeArchive()), ft.Writer()) == {"briefs_written": 1}


@pytest.mark.parametrize("batched", [True, False], ids=["batched", "late"])
@pytest.mark.parametrize("action", ["now", "pdf"])
async def test_during_a_nightly_the_owners_pass_is_the_workers_at_once(
    conn: Session, monkeypatch, action, batched
):
    """V8: "run it now" and "Use the PDF" make the pass the owner's: the nightly leaves it,
    batching or not (`late`: past its deadline), and the service's worker runs it with a normal
    call while the nightly holds the lock."""
    read = PdfText("From the PDF. " * 40, ((0, 560),))
    monkeypatch.setattr("litwatch.archive.text.read_pdf", lambda pdf: read)
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
    until = None if batched else datetime.now(UTC)
    async with nightly_running(conn):
        assert await batching_work(conn, writer, fake, anthropic, until) == {}
        assert await ft.work(conn, fake, writer) == {"briefs_written": 1}
    assert anthropic.batches == {}


async def test_topics_digests_written_together_are_those_written_one_after_another(conn: Session):
    """V11: a Monday's digests through batches, topics concurrently, equal those written in
    turn: a failed digest written again before the day's, the weeklies after the dailies."""

    async def monday(c: Session, together: bool) -> tuple[list, BatchingProvider | None]:
        await settling.save_tree(c, TREE)
        await in_write(c, papers_store.upsert, [dg.P, dg.Q, dg.R, dg.S])
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
        tree = await in_read(c, taxonomy_store.load)
        assert tree is not None
        async with gate_on(c) as db:
            gate = w.gate(Config())
            if together:
                anthropic = BatchingProvider("anthropic", w.respond)
                gate = factory(w.respond, anthropic, db, Config())
            async with gate, model_run(db, "nightly", models=gate) as run:
                if together:
                    with spending(deadline=tonight()[1]):
                        await digests.nightly(db, run, tree, dg.MON, rules=settling.RULES)
                else:
                    await digests.retry_failed(db, run, tree, rules=settling.RULES)
                    await digests.write_day(db, run, tree, dg.MON, rules=settling.RULES)
                    week = dg.MON - timedelta(days=7)
                    await digests.write_week(db, run, tree, week, rules=settling.RULES)
        rows = await (
            await c.execute("SELECT topic, period, day, state, text FROM digests ORDER BY 1, 2, 3")
        ).fetchall()
        found = [(*r.values(), await dg.shown(c, r["topic"], r["period"], r["day"])) for r in rows]
        return found, anthropic

    expected, _ = await monday(conn, together=False)
    async with sessions.schema(conn.test_dsn) as other:  # a second one
        found, anthropic = await monday(other, together=True)
    assert found == expected
    assert sum(r[3] == "written" for r in found) >= 3
    assert anthropic.calls == [] and len(anthropic.batches) <= 3  # rounds, not one per digest


# -- OpenAI's Batch API ----------------------------------------------------------------------------

SOL = RoleConfig(provider="openai", model="gpt-6.2-sol", effort="high")
# What the fake's answer to a batched request is charged: half of a normal call's 1000 fresh
# input tokens at $2, 3000 cached at $0.10 and 200 output at $10.
SOL_BATCHED = 0.00215
FAILED = {"code": "server_error", "message": "x"}


def openai_batching(journal=None, client=None, clock=None, models=None):
    """A run's door batching on `openai` until DEADLINE, over a faked Batch API."""
    clock, client = clock or Clock(), client or OpenAIBatches()
    providers: dict[str, Provider] = {"openai": OpenAIProvider(client=client)}
    options = FAST | {"now": clock}
    served = models or Models(brief=SOL)
    gate = ModelGate(served, providers, journal, retry_delays=(0.0,), batch_options=options)
    return Tonight(gate, journal), client, clock


async def test_openai_batches_carry_the_request_key_and_are_priced_at_half():
    # A U+2028 inside an answer's string does not end its line.
    answer = openai_body(json.dumps({"yes": True, "why": "a\u2028b"}, ensure_ascii=False))
    client = OpenAIBatches(lambda body: answer)
    provider, staged, request = OpenAIProvider(client=client), Staged(), ask(1)
    batch_id = await provider.submit([("k1", SOL, request)], staged)
    assert staged == ["file-1"]  # its input file, before the batch was made
    body = responses_request(SOL, request)
    assert client.uploaded["file-1"] == [
        {"custom_id": "k1", "method": "POST", "url": "/v1/responses", "body": body}
    ]
    assert await provider.status(batch_id) == "in_progress"
    client.end(batch_id)
    assert await provider.status(batch_id) == "ended"
    reply = (await provider.results(batch_id))["k1"]
    assert isinstance(reply, Reply) and json.loads(reply.text)["why"] == "a\u2028b"
    usage = reply.usage
    # Priced by the alias its snapshot answered as.
    assert (usage.model, usage.service_tier, usage.via) == ("gpt-6.2-sol", "batch", "openai")
    assert usage.cost == pytest.approx(SOL_BATCHED)
    assert await provider.status("batch_deleted") == "gone"


async def test_openai_batch_lines_that_failed_are_asked_again_refused_or_fail_on_the_account():
    answers = {
        "paper ok": openai_body(GOOD),
        "paper refused": openai_body("", refusal="I can't help with that."),
        "paper declined": (400, {"code": "invalid_prompt", "message": "flagged"}),
        "paper flagged": openai_body(status="failed", error={"code": "bio_policy"}),
        "paper server": (500, FAILED),
        "paper rate": (429, {"code": "rate_limit_exceeded"}),
        "paper expired": (None, {"code": "batch_expired"}),
        "paper failed": openai_body(status="failed", error=FAILED),
        "paper key": (401, {"code": "invalid_api_key"}),
        "paper credit": (429, {"code": "credit_balance_exhausted", "type": "insufficient_quota"}),
    }
    client = OpenAIBatches(lambda body: answers[body["input"][1]["content"]])
    provider = OpenAIProvider(client=client)
    requests = [(user, SOL, Request(system="rubric", user=user, output=Answer)) for user in answers]
    batch_id = await provider.submit(requests, Staged())
    client.end(batch_id)
    found = await provider.results(batch_id)
    replies = [found[f"paper {n}"] for n in ("ok", "refused", "declined", "flagged")]
    assert [(r.stop, r.refusal) if isinstance(r, Reply) else r for r in replies] == [
        (Stop.COMPLETE, None),
        (Stop.REFUSED, "unspecified"),
        (Stop.REFUSED, "invalid_prompt"),
        (Stop.REFUSED, "bio_policy"),
    ]
    assert isinstance(replies[3], Reply) and replies[3].usage.cost == pytest.approx(SOL_BATCHED)
    assert isinstance(replies[2], Reply)  # declined unbilled, at the batch's tier still
    assert (replies[2].usage.cost, replies[2].usage.service_tier) == (0.0, "batch")
    for user in ("paper server", "paper rate", "paper expired"):
        assert found[user] is None  # asked again as a normal call
    billed = found["paper failed"]  # billed and failed: asked again, its charge kept
    assert isinstance(billed, ProviderError) and not billed.account and billed.usage is not None
    assert billed.usage.cost == pytest.approx(SOL_BATCHED)
    for user in ("paper key", "paper credit"):
        error = found[user]
        assert isinstance(error, ProviderError) and error.account


def lost() -> Exception:
    return openai.APIConnectionError(request=httpx2.Request("POST", "https://x.org"))


async def configured(conn: Session) -> list[tuple]:
    """Each call's model and effort, as its row keeps them."""
    rows = await conn.execute(
        "SELECT usage->>'model' AS model, usage->>'effort' AS effort FROM inference_calls"
    )
    return [(r["model"], r["effort"]) for r in await rows.fetchall()]


async def test_an_openai_batch_is_found_by_its_file_through_every_page_of_the_sdks_list():
    client = OpenAIBatches(page=1)
    client.creating = [(True, lost())]  # created, and the reply lost on the way back
    provider = OpenAIProvider(client=client)
    with pytest.raises(ProviderError) as raised:
        await provider.submit([("k1", SOL, ask(1)), ("k2", SOL, ask(2))], Staged())
    assert raised.value.unanswered and list(client.made) == ["batch_1"]
    for n in (3, 4):  # newer batches, listed before it
        await provider.submit([(f"k{n}", SOL, ask(n))], Staged())
    client.end("batch_1")
    # Found by its file, ended, three pages down.
    assert await provider.find("file-1") == "batch_1"
    assert await provider.find("file-9") is None  # no batch was made of it
    assert await provider.find(None) is None


@pytest.mark.parametrize("failure", ["server", "lost", "hang"])
async def test_an_openai_batch_whose_creation_may_have_failed_is_adopted_once_found(
    conn: Session, db, failure
):
    client = OpenAIBatches()  # no normal call can be made (`responses`)
    error = {"server": status_error(openai, 500), "lost": lost(), "hang": "hang"}[failure]
    client.creating, client.misses = [(True, error)], 1  # and the first lookup misses it
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    gw.batchers["openai"].operation = 0.2  # our own bound, past which a hang is unknown too
    asked = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: client.listings == 2)
    client.end("batch_1")
    assert (await asked).usage.service_tier == "batch"
    assert [(c["batch_id"], c["cost"]) for c in await calls(conn)] == [
        ("batch_1", pytest.approx(SOL_BATCHED))
    ]
    await gw.close()


async def test_an_openai_batch_never_found_sends_its_calls_normally_once_the_window_ends(
    conn: Session, db, caplog
):
    client = OpenAIBatches()
    client.creating, client.unlisted = [(True, lost())], True  # and every lookup fails
    client.responses = SimpleNamespace(create=SdkClient(openai_response(GOOD)).responses.create)
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    assert (await gw.ask("brief", ask(1))).usage.service_tier == "default"
    batched, normal = await calls(conn)
    assert batched["batch_id"] == "staged:file-1" and batched["cost"] is None
    assert batched["error"].startswith("provider: the batch may have been made")
    assert (normal["batch_id"], normal["answered"]) == (None, True)
    assert "staged:file-1 may have been made and was not found" in caplog.text
    await gw.close()


def never_connected() -> Exception:
    error = openai.APIConnectionError(request=httpx2.Request("POST", "https://x.org"))
    error.__cause__ = httpx2.ConnectError("refused")  # as the SDK raises it, from the transport's
    return error


@pytest.mark.parametrize("failure", ["400", "429", "never connected"])
async def test_an_openai_batch_the_api_refused_to_make_sends_its_calls_normally_at_once(
    conn: Session, db, failure
):
    client = OpenAIBatches()
    if failure == "never connected":
        error = never_connected()
    else:
        error = status_error(openai, int(failure), {"code": "x", "message": "no"})
    client.creating = [(False, error)]
    client.responses = SimpleNamespace(create=SdkClient(openai_response(GOOD)).responses.create)
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, _, _ = openai_batching(journal, client)
    assert (await gw.ask("brief", ask(1))).value.yes
    assert client.listings == 0  # not looked for
    batched, normal = await calls(conn)
    assert batched["batch_id"] == "staged:file-1" and batched["cost"] == 0.0  # charged nothing
    assert batched["error"].startswith("provider: the batch was not made")
    assert (normal["batch_id"], normal["answered"]) == (None, True)
    # Its reference settled, no identical call attaches to it.
    assert await journal.in_batch(gw.request_key(SOL, ask(1)), None) is None
    await gw.close()


async def test_an_identical_call_is_given_what_a_creation_still_looked_for_gives(conn: Session, db):
    client = OpenAIBatches()  # no normal call can be made (`responses`)
    client.creating, client.misses = [(True, lost())], 2
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    gw.batchers["openai"].find_delays = (0.05, 0.05, 0.05)
    first = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: client.listings == 1)  # its batch looked for, on its staged row
    second = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: client.listings == 3)
    client.end("batch_1")
    answers = await asyncio.gather(first, second)
    assert [(a.usage.service_tier, a.usage.reused) for a in answers] == [
        ("batch", False),
        ("batch", True),
    ]
    # One row, the send's: the shared answer names it, as a reused one does.
    (row,) = await (await conn.execute("SELECT id, batch_id FROM inference_calls")).fetchall()
    assert row["batch_id"] == "batch_1"
    assert [a.usage.source_call_id for a in answers] == [row["id"]] * 2
    await gw.close()


async def test_a_dead_runs_staged_batch_is_looked_for_its_id_kept_and_read(conn: Session, db):
    client = OpenAIBatches()
    client.creating = [(True, lost())]
    gw, _, clock = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    gw.batchers["openai"].find_delays = (30.0,)  # the run dies while it looks
    asked = asyncio.create_task(gw.ask("brief", ask(1)))

    async def staged():
        return [c["batch_id"] for c in await calls(conn)] == ["staged:file-1"]

    async def adopted():
        return [c["batch_id"] for c in await calls(conn)] == ["batch_1"]

    await until(staged)
    await until(lambda: "batch_1" in client.made)  # made, its reply lost
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()

    client.misses = 1  # the next run's first lookup misses it
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client, clock)
    await gw.recover()
    await until(adopted)  # kept before it is polled
    client.end("batch_1")
    await gw.settle()
    assert [c["cost"] for c in await calls(conn)] == [pytest.approx(SOL_BATCHED)]
    assert client.listings == 2
    await gw.close()


async def test_a_batch_whose_upload_outlasts_the_deadline_is_not_made(conn: Session, db):
    client = OpenAIBatches()
    client.responses = SimpleNamespace(create=SdkClient(openai_response(GOOD)).responses.create)
    gw, _, clock = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    client.uploaded_then = lambda: setattr(clock, "now", DEADLINE)
    answer = await asyncio.wait_for(gw.ask("brief", ask(1)), 5)
    assert (answer.usage.service_tier, client.made) == ("default", {})
    assert [(c["batch_id"], c["answered"]) for c in await calls(conn)] == [(None, True)]
    await gw.close()


async def test_an_openai_batch_timed_out_while_uploading_was_not_made(conn: Session, db, caplog):
    client = OpenAIBatches()
    client.responses = SimpleNamespace(create=SdkClient(openai_response(GOOD)).responses.create)

    async def hanging(**_):
        await asyncio.Event().wait()

    client.files.create = hanging
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    gw.batchers["openai"].operation = 0.1
    assert (await asyncio.wait_for(gw.ask("brief", ask(1)), 5)).value.yes
    assert "was not made; its calls go as normal calls" in caplog.text
    assert "may have been made" not in caplog.text and client.listings == 0
    assert [(c["batch_id"], c["answered"]) for c in await calls(conn)] == [(None, True)]
    await gw.close()


async def test_looking_for_a_batch_ends_with_its_window_however_long_a_lookup_takes(
    conn: Session, db
):
    client = OpenAIBatches()
    client.creating, client.hanging = [(True, lost())], True
    client.responses = SimpleNamespace(create=SdkClient(openai_response(GOOD)).responses.create)
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    batcher = gw.batchers["openai"]
    batcher.find_seconds, batcher.operation = 0.3, 30.0
    answer = await asyncio.wait_for(gw.ask("brief", ask(1)), 3)
    assert answer.usage.service_tier == "default"
    await gw.close()


async def test_an_account_error_looking_for_a_batch_fails_its_calls_and_leaves_it_to_recover(
    conn: Session, db
):
    client = OpenAIBatches()
    client.creating, client.listed = [(True, lost())], 401
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, _, _ = openai_batching(journal, client)
    with pytest.raises(CallError) as raised:
        await gw.ask("brief", ask(1))
    assert raised.value.kind == "account"
    assert [(c["batch_id"], c["error"]) for c in await calls(conn)] == [("staged:file-1", None)]
    observed = await conn.execute(
        "SELECT account_error FROM provider_observations WHERE provider = 'openai'"
    )
    assert "AuthenticationError" in (await observed.fetchone())["account_error"]
    (row,) = await (await conn.execute("SELECT id FROM inference_calls")).fetchall()
    left = {"batch": row["id"], "batch_id": "staged:file-1", "provider": "openai"}
    assert await journal.open_batches() == [left | {"live_until": None}]
    await gw.close()


async def test_a_call_given_up_on_while_its_batch_runs_has_its_row_charged(conn: Session, db):
    gw, client, _ = openai_batching(CallJournal(db, await started(conn, "nightly")))
    asked = asyncio.create_task(gw.ask("brief", ask(1)))

    async def sent():
        return [c["batch_id"] for c in await calls(conn)] == ["batch_1"]

    await until(sent)
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    client.end("batch_1")
    await gw.settle()
    assert [c["cost"] for c in await calls(conn)] == [pytest.approx(SOL_BATCHED)]
    await gw.close()


async def test_a_dated_snapshot_keeps_its_name_on_its_row_and_is_priced(conn: Session, db):
    dated = RoleConfig(provider="openai", model="gpt-6.2-sol-2026-11-01", effort="high")
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, client, _ = openai_batching(journal, models=Models(brief=dated))
    asked = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: client.made)
    client.end("batch_1")
    usage = (await asked).usage
    assert (usage.model, usage.cost) == (dated.model, pytest.approx(SOL_BATCHED))
    assert await configured(conn) == [(dated.model, "high")]
    await gw.close()


class Unmade:
    """Anthropic's `messages.batches`, failing to create a batch with `error`."""

    def __init__(self, error: Exception) -> None:
        self.error, self.created = error, 0

    async def create(self, requests):
        self.created += 1
        raise self.error


@pytest.mark.parametrize(
    ("code", "made"),
    [(500, "may have been made"), (529, "may have been made"), (400, "was not made")],
)
async def test_an_anthropic_batch_that_may_not_have_been_made_sends_its_calls_normally(
    conn: Session, db, caplog, code, made
):
    haiku = RoleConfig(provider="anthropic", model="claude-haiku-5-5", effort="medium")
    client = SdkClient(anthropic_message(GOOD))
    client.messages.batches = Unmade(status_error(anthropic, code))
    journal = CallJournal(db, await started(conn, "nightly"))
    providers: dict[str, Provider] = {"anthropic": AnthropicProvider(client=client)}
    # A window far longer than the test may take: Anthropic cannot find a batch, so never waits.
    options = FAST | {"now": Clock(), "find_delays": (60.0,)}
    gw = Tonight(ModelGate(Models(brief=haiku), providers, journal, batch_options=options), journal)
    assert (await asyncio.wait_for(gw.ask("brief", ask(1)), 5)).value.yes
    assert (client.messages.batches.created, len(client.sent)) == (1, 1)
    batched, normal = await calls(conn)
    assert batched["error"].startswith(f"provider: the batch {made}")
    assert (normal["batch_id"], normal["answered"]) == (None, True)
    # Never looked for (a Message Batch has nothing of ours to know it by), and told so.
    assert ("may have been made and cannot be found" in caplog.text) == (code != 400)
    await gw.close()


async def test_an_openai_batch_line_billed_and_failed_is_settled_on_its_row_and_asked_again(
    conn: Session, db
):
    client = OpenAIBatches(lambda body: openai_body(status="failed", error=FAILED))
    client.responses = SimpleNamespace(create=SdkClient(openai_response(GOOD)).responses.create)
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    asked = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: len(client.made) == 1)
    client.end("batch_1")
    assert (await asked).value.yes
    batched, normal = await calls(conn)
    assert (batched["batch_id"], batched["cost"]) == ("batch_1", pytest.approx(SOL_BATCHED))
    assert batched["error"].startswith("provider: response failed")
    assert (normal["batch_id"], normal["error"], normal["answered"]) == (None, None, True)
    assert normal["cost"] == pytest.approx(2 * SOL_BATCHED)  # the normal call's full price
    assert await configured(conn) == [("gpt-6.2-sol", "high")] * 2  # as configured, both
    await gw.close()


async def test_an_openai_batch_holds_one_model_so_a_step_of_two_models_goes_in_two():
    luna = RoleConfig(provider="openai", model="gpt-6-luna")
    gw, client, _ = openai_batching(models=Models(brief=SOL, writer=luna))
    asked = [
        asyncio.create_task(gw.ask(role, ask(n)))
        for n, role in enumerate(("brief", "writer", "brief"))
    ]
    await until(lambda: len(client.made) == 2)
    for batch_id in list(client.made):
        client.end(batch_id)
    answers = await asyncio.gather(*asked)
    sent = sorted([line["body"]["model"] for line in lines] for lines in client.uploaded.values())
    assert sent == [["gpt-6-luna"], ["gpt-6.2-sol", "gpt-6.2-sol"]]
    # Each priced as its own model: Luna's 1000 fresh at $0.10, 3000 cached at $0.01, 200 out
    # at $0.50, halved.
    assert [(a.usage.model, a.usage.service_tier) for a in answers[:2]] == [
        ("gpt-6.2-sol", "batch"),
        ("gpt-6-luna", "batch"),
    ]
    assert [a.usage.cost for a in answers[:2]] == [
        pytest.approx(SOL_BATCHED),
        pytest.approx(0.000115),
    ]
    await gw.close()


async def dead_batch(conn: Session, db, client: OpenAIBatches, models=None):
    """Two calls a dead run left in an open batch; then a gateway of a later run, recovering
    it. The gateway, its clock and the batch's id."""
    dead = await started(conn, "nightly")
    gw, _, clock = openai_batching(Together(db, dead, 2), client, models=models)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in range(2)]

    async def recorded():
        rows = await calls(conn)
        return len(rows) == 2 and all(c["batch_id"] == "batch_1" for c in rows)

    await until(recorded)
    for t in asked:  # the run dies with its batch open
        t.cancel()
    await asyncio.gather(*asked, return_exceptions=True)
    await gw.close()
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, _, _ = openai_batching(journal, client, clock, models=models)
    await gw.recover()
    return gw, journal, dead


async def test_an_openai_batch_a_dead_run_left_is_read_once_and_credited_to_its_calls(
    conn: Session, db
):
    client = OpenAIBatches()
    gw, _, dead = await dead_batch(conn, db, client)
    settling = asyncio.create_task(gw.settle())  # as a nightly does before it ends
    await asyncio.sleep(0.05)
    assert not settling.done()
    client.end("batch_1")
    await settling
    assert [(c["run_id"], c["batch_id"], c["answered"]) for c in await calls(conn)] == [
        (dead, "batch_1", False)
    ] * 2
    assert [c["cost"] for c in await calls(conn)] == [pytest.approx(SOL_BATCHED)] * 2
    assert client.read == [client.made["batch_1"].output_file_id]  # read once, nothing paid again
    assert await configured(conn) == [("gpt-6.2-sol", "high")] * 2  # as configured, not answered
    await gw.close()


async def test_an_openai_batch_that_failed_with_no_answers_settles_a_dead_runs_calls(
    conn: Session, db
):
    client = OpenAIBatches()
    gw, journal, _ = await dead_batch(conn, db, client)
    client.end("batch_1", failed=True)  # its input refused: no files at all
    await gw.settle()
    errors = [c["error"] for c in await calls(conn)]
    assert errors == ["provider: the batch did not answer it"] * 2
    assert await journal.open_batches() == []
    await gw.close()


async def test_an_openai_answer_of_unknown_charge_is_read_once_and_still_given_to_its_call(
    conn: Session, db
):
    unlisted = Models(brief=RoleConfig(provider="openai", model="gpt-9"))
    client = OpenAIBatches()
    gw, journal, _ = await dead_batch(conn, db, client, unlisted)
    client.end("batch_1")
    await gw.settle()
    assert [c["cost"] for c in await calls(conn)] == [None, None]
    assert await journal.open_batches() == []  # accounted for, though its charge is unknown
    await gw.close()
    # A run asking for one of them again is given its answer, not a new call.
    gw, _, _ = openai_batching(
        CallJournal(db, await started(conn, "nightly")), client, None, unlisted
    )
    assert (await gw.ask("brief", ask(0))).value.yes
    assert (len(client.made), len(await calls(conn))) == (1, 2)
    await gw.close()


# -- a batch's outcome, settled once ---------------------------------------------------------------


async def test_a_batch_whose_rows_open_as_its_deadline_passes_is_not_made(
    conn: Session, db, monkeypatch
):
    """The deadline is looked at again once the batch's rows are open, as opening them takes
    time: past it, the batch is not made, its rows settle unbilled and its calls go normally."""
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, anthropic, _, clock = batching(journal)
    start_many = journal.start_many

    async def slow(calls, batch_id=None, registered=None):
        opened = await start_many(calls, batch_id, registered)
        clock.now = DEADLINE  # opening them took until the deadline
        return opened

    monkeypatch.setattr(journal, "start_many", slow)
    answer = await gw.ask("brief", ask(0))
    assert answer.usage.service_tier is None and anthropic.batches == {}
    unmade, normal = await calls(conn)
    assert (unmade["error"], unmade["cost"]) == (
        "provider: the batch was not made: its deadline passed",
        0.0,
    )
    assert (normal["batch_id"], normal["answered"]) == (None, True)
    await gw.close()


async def test_a_batchs_rows_open_together_naming_its_reference_before_it_is_made(
    conn: Session, db
):
    """In one write, so a process that dies at any point leaves either no row or every row on
    the reference its batch can be found by."""
    client = OpenAIBatches()
    create, seen = client.batches.create, []

    async def watched(**kwargs):
        seen.append([c["batch_id"] for c in await calls(conn)])
        return await create(**kwargs)

    client.batches.create = watched
    gw, _, _ = openai_batching(Together(db, await started(conn, "nightly"), 2), client)
    asked = asyncio.gather(gw.ask("brief", ask(1)), gw.ask("brief", ask(2)))
    await until(lambda: client.made)
    client.end("batch_1")
    await asked
    assert seen == [["staged:file-1", "staged:file-1"]]
    await gw.close()


async def test_a_batch_whose_creation_may_have_failed_is_looked_for_at_once_and_adopted(
    conn: Session, db, monkeypatch
):
    """Its lookup starts as its creation fails, in its own batch's task: another batch's read,
    hung past the whole window, holds it up not at all, and the batch, made after all, is found,
    adopted and read."""
    client = OpenAIBatches()  # no normal call can be made (`responses`)
    client.creating = [(True, None), (True, lost())]  # the second's reply lost on the way back
    retrieve, reads, reading = client.batches.retrieve, [], asyncio.Event()

    async def hung(batch_id):
        reads.append(batch_id)
        if batch_id == "batch_1":
            await reading.wait()
        return await retrieve(batch_id)

    monkeypatch.setattr(client.batches, "retrieve", hung)
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    gw.batchers["openai"].find_seconds = 0.5
    first = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: "batch_1" in reads)  # its read hangs
    second = asyncio.create_task(gw.ask("brief", ask(2)))

    async def adopted() -> bool:
        return [c["batch_id"] for c in await calls(conn)] == ["batch_1", "batch_2"]

    await until(adopted, 2)
    assert client.listings == 1
    client.end("batch_2")
    assert (await asyncio.wait_for(second, 2)).usage.service_tier == "batch"
    reading.set()
    client.end("batch_1")
    assert (await asyncio.wait_for(first, 2)).usage.service_tier == "batch"
    await gw.close()


async def test_a_batched_refusal_is_settled_on_its_row_though_its_call_was_cancelled(
    conn: Session, db
):
    """Its stop and category are the batch's outcome, persisted by the batcher, not by the call
    that waited on it."""
    gw, anthropic, _, _ = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    asked = asyncio.create_task(gw.ask("brief", ask("refuse")))
    await until(batch_recorded(conn))
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    anthropic.end("msgbatch_1")
    await gw.settle()
    ((row),) = await calls(conn)
    assert (row["error"], row["cost"], row["answered"]) == ("refused: cyber", 0.001, False)
    await gw.close()


async def test_an_identical_call_saves_its_answer_on_the_row_its_cancelled_first_call_opened(
    conn: Session, db
):
    """It shares the first call's request, and is given that row's id with its reply, so its
    validated output is saved there."""
    gw, anthropic, _, _ = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    first = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(batch_recorded(conn))
    second = asyncio.create_task(gw.ask("brief", ask(1)))
    await asyncio.sleep(0.05)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    anthropic.end("msgbatch_1")
    answer = await second
    (row,) = await (await conn.execute("SELECT id, output FROM inference_calls")).fetchall()
    assert answer.usage.reused and answer.usage.source_call_id == row["id"]
    assert row["output"] == {"yes": True, "why": "it says so"}
    await gw.close()


def failing_tally(unit, run_id):
    raise OSError("the database went away")


async def test_a_batch_settlement_that_fails_part_way_leaves_it_whole_for_the_next_process(
    conn: Session, db, monkeypatch
):
    """Its calls' outcomes and their runs' totals commit together: a settlement that fails on
    a run's tally leaves its rows as they were, so the next process settles them, and the run's
    cost is then its call's charge."""
    anthropic, clock = await dead_batches(conn, db, 1)
    tally = calls_store.tally
    monkeypatch.setattr(calls_store, "tally", failing_tally)
    gw, _, _, _ = batching(CallJournal(db, await started(conn, "nightly")), provider=anthropic)
    await gw.recover()
    await gw.settle()
    await gw.close()
    (row,) = await calls(conn)
    assert row["cost"] is None  # nothing of it committed

    monkeypatch.setattr(calls_store, "tally", tally)
    again, _, _, _ = batching(CallJournal(db, await started(conn, "nightly")), provider=anthropic)
    await again.recover()
    await again.settle()
    assert [c["cost"] for c in await calls(conn)] == [0.0005]
    found = await in_read(conn, runs.get, row["run_id"])
    assert found is not None and float(found["cost"]) == 0.0005
    await again.close()


async def test_a_batch_refused_for_the_account_as_it_is_made_is_observed_with_its_rows(
    conn: Session, db, monkeypatch
):
    """The provider refused the batch's creation for the account, its rows opened: the refusal
    is observed in the same unit that settles them, so neither stands without the other."""
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, anthropic, _, _ = batching(journal)

    async def refused(requests, staged):
        await staged(None)
        raise ProviderError("AuthenticationError: invalid x-api-key", account=True)

    monkeypatch.setattr(anthropic, "submit", refused)
    tally = calls_store.tally
    monkeypatch.setattr(calls_store, "tally", failing_tally)
    with pytest.raises(OSError):
        await gw.ask("brief", ask(0))
    assert "anthropic" not in await in_read(conn, calls_store.observations)
    assert [(c["error"], c["cost"]) for c in await calls(conn)] == [(None, None)]

    monkeypatch.setattr(calls_store, "tally", tally)
    with pytest.raises(CallError) as found:
        await gw.ask("brief", ask(1))
    assert found.value.kind == "account"
    seen = (await in_read(conn, calls_store.observations))["anthropic"]
    assert "invalid x-api-key" in seen["account_error"]
    assert (await calls(conn))[1]["error"] == "account: AuthenticationError: invalid x-api-key"
    await gw.close()


async def test_a_settled_batch_read_again_does_not_observe_its_account_error_again(
    conn: Session, db
):
    """A dead run's batch answered one request and refused the other for the account: its
    settlement observes that once. A call that asks for the answer no call awaited reads the
    batch again, which settles nothing more, so a reply the provider gave since stands."""
    dead = CallJournal(db, await started(conn, "nightly"))
    gw, anthropic, _, clock = batching(dead, hold=True)
    asked = [asyncio.create_task(gw.ask("brief", ask(n))) for n in (0, "account")]
    await until(batch_recorded(conn))
    for task in asked:
        task.cancel()
    await asyncio.gather(*asked, return_exceptions=True)
    await gw.close()
    anthropic.end("msgbatch_1")

    later, _, _, _ = batching(
        CallJournal(db, await started(conn, "nightly")), provider=anthropic, clock=clock
    )
    await later.recover()
    await later.settle()
    await later.close()
    first = (await in_read(conn, calls_store.observations))["anthropic"]
    assert "authentication_error" in first["account_error"]
    await conn.execute("UPDATE provider_observations SET replied_at = clock_timestamp()")

    journal = CallJournal(db, await started(conn, "nightly"))
    asking, _, _, _ = batching(journal, provider=anthropic, clock=clock, deadline=None)
    assert (await asking.ask("brief", ask(0))).usage.service_tier == "batch"
    seen = (await in_read(conn, calls_store.observations))["anthropic"]
    assert seen["account_error_at"] == first["account_error_at"] < seen["replied_at"]
    await asking.close()


async def test_a_batched_truncation_keeps_its_text_on_its_row_in_debug_mode(conn: Session, db):
    """As a normal call's does: its batch's settlement keeps it, as no call saves the row."""
    gw, _, _, _ = batching(CallJournal(db, await started(conn, "nightly")))
    gw.gate.debug = True  # before its batcher is made, which takes it
    with pytest.raises(CallError) as found:
        await gw.ask("brief", ask("truncate"))
    assert found.value.kind == "truncated"
    row = await (await conn.execute("SELECT error, reply FROM inference_calls")).fetchone()
    assert row == {"error": "truncated", "reply": '{"yes": tr'}
    await gw.close()


# -- one identity, one owner -----------------------------------------------------------------------


def a_work_item(item_id: int) -> Work:
    """The work context of item `item_id`, as its Worker sets it (its journal writes are the
    gate's bookkeeping, so its lease is not looked at)."""
    item = WorkItem(
        id=item_id,
        kind="command",
        key=f"run {item_id}",
        admission="x",
        state=ItemState.RUNNING,
        not_before=START,
        attempts=1,
    )
    return Work(item)


async def item_of(conn: Session, run_id: int) -> int:
    row = await (await conn.execute("SELECT work_id FROM runs WHERE id = %s", (run_id,))).fetchone()
    return row["work_id"]


async def test_an_item_asking_again_takes_the_refusal_its_dead_attempts_batch_gave(
    conn: Session, db
):
    """The item's attempt is killed with its call in a batch, which refuses it; recovery
    settles the refusal on its row. Asking again, the item takes that refusal (down to the
    role's fallback) rather than asking the same model again; another item, an explicit retry,
    asks it."""
    dead = await started(conn, "nightly", alive=True)
    item = await item_of(conn, dead)
    gw, anthropic, openrouter, clock = batching(CallJournal(db, dead), hold=True)
    with working(a_work_item(item)):
        asked = asyncio.create_task(gw.ask("brief", ask("refuse")))
    await until(batch_recorded(conn))
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()
    await lease_lapses(conn, dead)
    anthropic.end("msgbatch_1")
    later, _, _, _ = batching(CallJournal(db), provider=anthropic, clock=clock)
    await later.recover()
    await later.settle()
    await later.close()
    assert [c["error"] for c in await calls(conn)] == ["refused: cyber"]

    again = await started(conn, "nightly")
    gw, _, _, _ = batching(CallJournal(db, again), provider=anthropic, clock=clock)
    gw.gate.providers["openrouter"] = openrouter
    with working(a_work_item(item)):  # its batch would be held, never answering
        answer = await asyncio.wait_for(gw.ask("brief", ask("refuse")), 3)
    assert answer.model == OTHER.label and len(anthropic.batches) == 1  # not asked again
    # Another item's (an explicit retry) is not given it.
    assert await CallJournal(db).in_batch(gw.request_key(BRIEF, ask("refuse")), None) is None
    await gw.close()


async def test_a_batch_settlement_the_database_briefly_failed_is_tried_again_its_answer_kept(
    conn: Session, db, monkeypatch
):
    """The connection is lost as an ended batch's settlement retallies its run: the unit rolls
    back, and the batch's task writes the same settlement again, so the charge is not left
    unknown until another process reads the batch."""
    anthropic, clock = await dead_batches(conn, db, 1)
    tally, lost_once = calls_store.tally, []

    async def drops(unit, run_id):
        if not lost_once:
            lost_once.append(run_id)
            raise psycopg.OperationalError("the server closed the connection unexpectedly")
        await tally(unit, run_id)

    monkeypatch.setattr(calls_store, "tally", drops)
    results, read = anthropic.results, []

    async def counted(batch_id):
        read.append(batch_id)
        return await results(batch_id)

    monkeypatch.setattr(anthropic, "results", counted)
    gw, _, _, _ = batching(CallJournal(db), provider=anthropic, clock=clock)
    await gw.recover()
    await gw.settle()
    (row,) = await calls(conn)
    assert lost_once and row["cost"] == 0.0005 and read == ["msgbatch_1"]  # read once
    found = await in_read(conn, runs.get, row["run_id"])
    assert found is not None and float(found["cost"]) == 0.0005
    await gw.close()


async def test_a_held_request_every_call_stopped_waiting_on_is_never_sent(conn: Session, db):
    """Two calls are held, one of them asked twice; the one asked once stops waiting (its lease
    lost, say), and one of the two asking the other: only the request still wanted is sent, on
    the one row its batch opens, and answered."""
    gw, anthropic, _, _ = batching(CallJournal(db, await started(conn, "nightly")))
    gw.batchers["anthropic"].quiet = 0.5
    alone = asyncio.create_task(gw.ask("brief", ask(0)))
    first = asyncio.create_task(gw.ask("brief", ask(1)))
    identical = asyncio.create_task(gw.ask("brief", ask(1)))
    await asyncio.sleep(0.2)  # all held, the batch not yet sent
    assert anthropic.batches == {}
    alone.cancel()
    first.cancel()
    await asyncio.gather(alone, first, return_exceptions=True)
    answer = await asyncio.wait_for(identical, 3)
    assert anthropic.submitted == [[gw.request_key(BRIEF, ask(1))]]
    assert answer.usage.service_tier == "batch"
    ((row),) = await (await conn.execute("SELECT id, output FROM inference_calls")).fetchall()
    assert answer.usage.source_call_id == row["id"] and row["output"] is not None
    await gw.close()


async def test_a_call_without_a_deadline_looks_once_at_a_batch_recovery_follows(conn: Session, db):
    """Recovery follows a dead nightly's batch until tonight's deadline; a job's call for the
    same request looks at it once, at once, and is sent normally while the batch runs on,
    neither cancelled nor given up on, and accounted for once it ends."""
    gw, anthropic, _, clock = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(batch_recorded(conn))
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()

    recovering, _, _, _ = batching(CallJournal(db), provider=anthropic, clock=clock)
    assert await recovering.recover() == 1
    job = Tonight(recovering.gate, CallJournal(db, await started(conn, "job")), deadline=None)
    answer = await asyncio.wait_for(job.ask("brief", ask(0)), 2)
    assert answer.usage.service_tier is None and len(anthropic.calls) == 1
    assert anthropic.cancelled == []
    anthropic.end("msgbatch_1")
    await recovering.settle()
    assert [c["cost"] for c in await calls(conn)] == [0.0005, 0.001]
    await recovering.close()


async def test_a_live_commands_batch_found_meanwhile_is_recovered_by_its_identity(
    conn: Session, db, monkeypatch
):
    """`serve` starts while a live command's OpenAI batch is known by its staged reference;
    the command finds its id, puts it on the rows, and dies before reading it. Its lease
    lapsed, `serve` takes the batch over by its identity, under the id it goes by now, and
    reads it once: a first look again that fails on the database is tried again."""
    command = await started(conn, "nightly", alive=True)
    client = OpenAIBatches()
    client.creating = [(True, lost())]
    provider = OpenAIProvider(client=client)
    with pytest.raises(ProviderError):
        await provider.submit([("k1", SOL, ask(1))], Staged())
    journal = CallJournal(db)
    start = CallStart(
        run_id=command, work_id=await item_of(conn, command), operation="brief",
        model=SOL.model, key="k1", via="openai",
    )  # fmt: skip
    await journal.start_many([start], "staged:file-1")
    await conn.execute(
        "UPDATE work_items SET lease_until = clock_timestamp() + interval '1 second'"
    )
    gw, _, _ = openai_batching(journal, client)
    listed, looked = journal.open_batches, []

    async def flaky(batches=None):
        looked.append(batches)
        if len(looked) == 2:
            raise psycopg.OperationalError("the server closed the connection unexpectedly")
        return await listed(batches)

    monkeypatch.setattr(journal, "open_batches", flaky)
    assert await gw.recover() == 0  # the command's, while its lease is live
    await journal.adopted("staged:file-1", "batch_1")  # the command found it, then died
    client.end("batch_1")

    async def credited() -> bool:
        return [c["cost"] for c in await calls(conn)] == [pytest.approx(SOL_BATCHED)]

    await until(credited, 6)
    assert len(looked) >= 3 and client.listings == 0  # by its id, not looked for again
    await gw.close()


async def test_a_batch_whose_item_is_claimed_again_is_taken_over_from_the_dead_claim(
    conn: Session, db
):
    """The process dies with the item's call in a batch; the item is taken back under a new
    claim, which keeps it running: the batch is the dead claim's, no longer live, so `serve`
    takes it over as that claim's lease lapses, though the item is."""
    dead = await started(conn, "nightly", alive=True)
    gw, anthropic, _, clock = batching(CallJournal(db, dead), hold=True)
    with working(a_work_item(await item_of(conn, dead))):
        asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(batch_recorded(conn))
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()
    await conn.execute(
        "UPDATE work_items SET lease_until = clock_timestamp() + interval '1 second'"
    )
    anthropic.end("msgbatch_1")

    recovering, _, _, _ = batching(CallJournal(db), provider=anthropic, clock=clock)
    assert await recovering.recover() == 0
    await conn.execute(  # taken back: a new claim on the same item, live for an hour
        "UPDATE work_items SET lease_token = gen_random_uuid(), "
        "lease_until = clock_timestamp() + interval '1 hour'"
    )

    async def credited() -> bool:
        return [c["cost"] for c in await calls(conn)] == [0.0005]

    await until(credited, 6)
    await recovering.close()


async def test_a_call_that_reads_a_batchs_id_as_it_is_adopted_joins_its_task(
    conn: Session, db, monkeypatch
):
    """A batch's creation lost its reply; its task finds it and puts its id on its rows. A
    call without a deadline reads that id before the task polls by it: it joins the one task
    that owns the batch, which is never cancelled before its deadline."""
    client = OpenAIBatches()  # no normal call can be made (`responses`)
    client.creating = [(True, lost())]
    client.responses = SimpleNamespace(create=SdkClient(openai_response(GOOD)).responses.create)
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, _, _ = openai_batching(journal, client)
    adopted, published, polling = journal.adopted, asyncio.Event(), asyncio.Event()

    async def paused(reference, batch_id):
        await adopted(reference, batch_id)
        published.set()
        await polling.wait()

    monkeypatch.setattr(journal, "adopted", paused)
    asked = asyncio.create_task(gw.ask("brief", ask(1)))
    await asyncio.wait_for(published.wait(), 3)
    worker = Tonight(gw.gate, CallJournal(db, await started(conn, "job")), deadline=None)
    looking = asyncio.create_task(worker.ask("brief", ask(1)))
    await asyncio.sleep(0.1)
    polling.set()
    assert (await asyncio.wait_for(looking, 3)).value.yes  # a look, then a normal call
    assert client.made["batch_1"].status == "in_progress"  # not cancelled
    client.end("batch_1")
    assert (await asyncio.wait_for(asked, 3)).usage.service_tier == "batch"
    await gw.close()


async def test_a_run_past_its_deadline_waits_for_a_dead_runs_batch_it_asks_again(conn: Session, db):
    """No `serve` has started since the run died: a nightly started after the deadline, under
    a deadline of its own start (`jobs.nightly`), attaches to the dead run's batch, cancels it
    and waits for it, keeping the answer it had; a call without a deadline would look once."""
    gw, anthropic, _, clock = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(batch_recorded(conn))
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()

    clock.now = DEADLINE + timedelta(hours=1)
    late = CallJournal(db, await started(conn, "nightly"))
    gw, _, _, _ = batching(late, provider=anthropic, clock=clock, deadline=clock.now)
    first = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(lambda: anthropic.cancelled == ["msgbatch_1"])
    await asyncio.sleep(0.05)
    assert not first.done()  # waiting for the cancelled batch, not sent again
    anthropic.end("msgbatch_1", answered={gw.request_key(BRIEF, ask(0))})
    assert (await first).usage.service_tier == "batch" and anthropic.calls == []
    await gw.close()


async def test_a_staged_batch_is_its_senders_before_any_call_can_read_its_rows(
    conn: Session, db, monkeypatch
):
    """A batch's rows commit as its creation is asked for. An identical call that reads them
    before its sender has heard back from opening them joins the sender's task, which owns the
    batch from before they were committed: no second task looks for the batch and settles it
    unfound while its provider makes it, and its answer is saved on its row."""
    client = OpenAIBatches()
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, _, _ = openai_batching(journal, client)
    start_many, committed, go = journal.start_many, asyncio.Event(), asyncio.Event()

    async def slow(calls, batch_id=None, opened=None):
        ids = await start_many(calls, batch_id, opened)
        committed.set()
        await go.wait()  # its sender not yet back from opening them
        return ids

    monkeypatch.setattr(journal, "start_many", slow)
    first = asyncio.create_task(gw.ask("brief", ask(1)))
    await asyncio.wait_for(committed.wait(), 3)
    second = asyncio.create_task(gw.ask("brief", ask(1)))
    await asyncio.sleep(0.2)  # past a lookup window's worth of lookups (FAST)
    go.set()
    await until(lambda: "batch_1" in client.made)
    client.end("batch_1")
    answers = await asyncio.wait_for(asyncio.gather(first, second), 3)
    assert [a.usage.service_tier for a in answers] == ["batch", "batch"]
    assert client.listings == 0  # never looked for
    (row,) = await (await conn.execute("SELECT error, output FROM inference_calls")).fetchall()
    assert row["error"] is None and row["output"] is not None
    await gw.close()


async def test_a_late_nightly_attaching_as_a_call_without_a_deadline_looks_keeps_its_drain(
    conn: Session, db, monkeypatch
):
    """A dead run's batch no one follows: a job's call without a deadline looks once, which
    cancels it, and a nightly started after its deadline attaches while that look is in
    flight. The job's call goes normally after its look; the nightly's waits for the batch
    through its own drain, which the job's limit does not cut short, and takes its answer."""
    gw, anthropic, _, clock = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(batch_recorded(conn))
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()

    clock.now = DEADLINE + timedelta(hours=1)
    late = CallJournal(db, await started(conn, "nightly"))
    nightly, _, _, _ = batching(late, provider=anthropic, clock=clock, deadline=clock.now)
    status, looking, release = anthropic.status, asyncio.Event(), asyncio.Event()

    async def slow(batch_id):
        looking.set()
        await release.wait()
        return await status(batch_id)

    monkeypatch.setattr(anthropic, "status", slow)
    job = Tonight(nightly.gate, CallJournal(db, await started(conn, "job")), deadline=None)
    looked = asyncio.create_task(job.ask("brief", ask(0)))
    await asyncio.wait_for(looking.wait(), 3)
    waiting = asyncio.create_task(nightly.ask("brief", ask(0)))
    await asyncio.sleep(0.05)
    release.set()
    assert (await asyncio.wait_for(looked, 3)).usage.service_tier is None
    await asyncio.sleep(0.1)
    assert not waiting.done() and anthropic.cancelled == ["msgbatch_1"]
    anthropic.end("msgbatch_1", answered={nightly.request_key(BRIEF, ask(0))})
    assert (await asyncio.wait_for(waiting, 3)).usage.service_tier == "batch"
    assert len(anthropic.calls) == 1  # the job's alone
    await nightly.close()


async def test_a_call_without_a_deadline_looks_once_at_a_live_items_batch_this_gate_follows(
    conn: Session, db
):
    """A live nightly's batch, which this gate follows, has its answer and is between polls: a
    job's call for the same request gets its look, which reads that answer, rather than being
    sent normally, as another process's live item's batch would leave it."""
    run = await started(conn, "nightly", alive=True)
    gw, anthropic, _, _ = batching(CallJournal(db, run), hold=True)
    gw.batchers["anthropic"].poll = 60.0
    with working(a_work_item(await item_of(conn, run))):
        asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(batch_recorded(conn))
    await asyncio.sleep(0.05)  # its first look made: the next in a minute
    anthropic.end("msgbatch_1")
    job = Tonight(gw.gate, CallJournal(db, await started(conn, "job")), deadline=None)
    answer = await asyncio.wait_for(job.ask("brief", ask(0)), 3)
    assert answer.usage.service_tier == "batch" and anthropic.calls == []
    assert (await asyncio.wait_for(asked, 3)).usage.service_tier == "batch"
    await gw.close()


async def test_a_reply_whose_journal_write_briefly_failed_is_written_again_not_bought_again(
    conn: Session, db, monkeypatch
):
    """The connection is lost as a batched reply's output is saved, and as a normal call's
    charge is recorded: each write is tried again with the answer kept, so the output is saved
    and the charge known, and nothing is asked again."""
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, anthropic, _, _ = batching(journal)
    lost: list[str] = []

    def once(name, write):
        async def flaky(*args, **kwargs):
            if name not in lost:
                lost.append(name)
                raise psycopg.OperationalError("the server closed the connection unexpectedly")
            return await write(*args, **kwargs)

        return flaky

    monkeypatch.setattr(journal, "accept", once("accept", journal.accept))
    monkeypatch.setattr(journal, "record", once("record", journal.record))
    assert (await asyncio.wait_for(gw.ask("brief", ask(1)), 3)).usage.service_tier == "batch"
    normal = Tonight(gw.gate, journal, deadline=None)
    assert (await asyncio.wait_for(normal.ask("brief", ask(2)), 3)).usage.service_tier is None
    assert lost == ["accept", "record"]
    assert [(c["cost"], c["answered"]) for c in await calls(conn)] == [
        (0.0005, True),
        (0.001, True),
    ]
    assert len(anthropic.calls) == len(anthropic.batched) == 1
    await gw.close()


async def test_a_call_without_a_deadline_is_let_go_after_a_lookup_misses_its_batch(
    conn: Session, db
):
    """A batch's creation lost its reply, and its task looks for it through its window: a job's
    call for the same request wakes the next lookup and, that missing the batch too, goes as a
    normal call, while the task looks on."""
    client = OpenAIBatches()
    client.creating, client.misses = [(True, lost())], 100
    client.responses = SimpleNamespace(create=SdkClient(openai_response(GOOD)).responses.create)
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    batcher = gw.batchers["openai"]
    batcher.find_seconds, batcher.find_delays = 30.0, (0.01, 10.0, 10.0)
    asked = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: client.listings == 1)  # missed once: the next lookup in 10 s
    job = Tonight(gw.gate, CallJournal(db, await started(conn, "job")), deadline=None)
    answer = await asyncio.wait_for(job.ask("brief", ask(1)), 3)
    assert answer.usage.service_tier != "batch" and client.listings == 2  # its look, a miss
    assert not asked.done()
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()


async def test_a_call_without_a_deadline_does_not_wait_on_a_batch_being_made(conn: Session, db):
    """An OpenAI batch's creation is under way, its rows open naming its file: a job's call for
    the same request goes as a normal call at once, as no look at a batch being made can end
    it."""
    client = OpenAIBatches()
    client.responses = SimpleNamespace(create=SdkClient(openai_response(GOOD)).responses.create)
    create, making, made = client.batches.create, asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        making.set()
        await made.wait()
        return await create(**kwargs)

    client.batches.create = slow
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    asked = asyncio.create_task(gw.ask("brief", ask(1)))
    await asyncio.wait_for(making.wait(), 3)
    job = Tonight(gw.gate, CallJournal(db, await started(conn, "job")), deadline=None)
    answer = await asyncio.wait_for(job.ask("brief", ask(1)), 3)
    assert answer.usage.service_tier != "batch" and not asked.done()
    made.set()
    await until(lambda: "batch_1" in client.made)
    client.end("batch_1")
    assert (await asyncio.wait_for(asked, 3)).usage.service_tier == "batch"
    await gw.close()


async def test_a_refusal_a_p3_settlement_left_uncategorised_goes_on_its_row_when_read(
    conn: Session, db
):
    """P3 kept a batched reply's charge and time on its row, not its refusal, when no call
    waited on it. Read again, the refusal goes on that row, its charge and time kept, so the
    request is known refused; the item takes the role's fallback."""
    gw, anthropic, _, _ = batching(CallJournal(db, await started(conn, "nightly")))
    key = gw.request_key(BRIEF, ask("refuse"))
    batch_id = await anthropic.submit([(key, BRIEF, ask("refuse"))])
    start = CallStart(
        run_id=await started(conn, "nightly"), work_id=None, operation="brief",
        model=BRIEF.model, key=key, via="anthropic",
    )  # fmt: skip
    (row_id,) = await CallJournal(db).start_many([start], batch_id)
    replied = datetime(2026, 10, 16, 8, tzinfo=UTC)
    await conn.execute(
        "UPDATE inference_calls SET usage = usage || '{\"cost\": 0.0005}', responded_at = %s "
        "WHERE id = %s",
        (replied, row_id),
    )
    answer = await asyncio.wait_for(gw.ask("brief", ask("refuse")), 3)
    assert answer.model == OTHER.label
    found = "SELECT error, (usage->>'cost')::float AS cost, responded_at FROM inference_calls"
    row = await (await conn.execute(f"{found} WHERE id = %s", (row_id,))).fetchone()
    assert (row["error"], row["cost"], row["responded_at"]) == ("refused: cyber", 0.0005, replied)
    assert await CallJournal(db).refused(key, None)
    await gw.close()


async def test_a_nightly_attaching_to_a_batch_a_look_took_over_waits_as_its_deadline_says(
    conn: Session, db
):
    """A job's call without a deadline found a dead run's batch no one followed, which is then
    settled as at a deadline: cancelled at once, its drain from then. Tonight's nightly asks the
    same request: the batch is waited for as the nightly's deadline says, past that drain."""
    gw, anthropic, _, clock = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(batch_recorded(conn))
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()

    nightly, _, _, _ = batching(
        CallJournal(db, await started(conn, "nightly")), provider=anthropic, clock=clock
    )
    anthropic.respond = lambda role, request: '{"yes": tr'  # its normal call, unusable
    job = Tonight(nightly.gate, CallJournal(db, await started(conn, "job")), deadline=None)
    with pytest.raises(CallError, match="invalid"):
        await asyncio.wait_for(job.ask("brief", ask(0)), 3)
    assert anthropic.cancelled == ["msgbatch_1"]
    waiting = asyncio.create_task(nightly.ask("brief", ask(0)))
    await asyncio.sleep(0.05)
    clock.now = START + DRAIN + timedelta(minutes=1)  # the look's drain over, tonight's ahead
    await asyncio.sleep(0.1)
    assert not waiting.done()
    anthropic.end("msgbatch_1", answered={nightly.request_key(BRIEF, ask(0))})
    assert (await asyncio.wait_for(waiting, 3)).usage.service_tier == "batch"
    await nightly.close()


async def test_a_normal_calls_charge_is_recorded_though_its_item_is_cancelled_meanwhile(
    conn: Session, db, monkeypatch
):
    """A normal call's reply came; writing its charge lost the connection, and the write waits
    to be tried again when the item is cancelled (`serve` stopping), then cancelled again (its
    lease renewal failing): the write is finished first, so the charge the provider made is on
    its row, and the item is cancelled after, with why it was first."""
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, anthropic, _, _ = batching(journal)
    gw.gate.journal_delays = (0.3,)
    record, failing = journal.record, asyncio.Event()

    async def flaky(*args, **kwargs):
        if not failing.is_set():
            failing.set()
            raise psycopg.OperationalError("the server closed the connection unexpectedly")
        return await record(*args, **kwargs)

    monkeypatch.setattr(journal, "record", flaky)
    asked = asyncio.create_task(Tonight(gw.gate, journal, deadline=None).ask("brief", ask(1)))
    await asyncio.wait_for(failing.wait(), 3)
    asked.cancel("serve stopped while it ran")
    await asyncio.sleep(0.05)
    asked.cancel()  # again, as the write waits to be tried again
    with pytest.raises(asyncio.CancelledError, match="serve stopped while it ran"):
        await asyncio.wait_for(asked, 3)
    assert [c["cost"] for c in await calls(conn)] == [0.001] and len(anthropic.calls) == 1
    await gw.close()


async def test_a_call_that_read_a_batchs_staged_name_follows_it_by_the_id_it_has_since(
    conn: Session, db, monkeypatch
):
    """A call reads a batch's row while the batch goes by its staged reference; meanwhile the
    batch's task finds it, puts its id on its rows, reads it and lets it go. The call, finding
    no task following it, claims it and reads its name again before any provider request: it
    reads the batch by its id rather than looking for it again, which may miss it, and pays for
    nothing twice."""
    client = OpenAIBatches()  # no normal call can be made (`responses`)
    client.creating = [(True, lost())]
    journal = CallJournal(db, await started(conn, "nightly"))
    gw, _, _ = openai_batching(journal, client)
    in_batch, adopted, read, go = (
        journal.in_batch,
        journal.adopted,
        asyncio.Event(),
        asyncio.Event(),
    )

    async def paused(key, work_id, owned=None):
        row = await in_batch(key, work_id, owned)
        if row is not None and row["batch_id"].startswith("staged:"):
            read.set()
            await go.wait()
        return row

    async def after_the_read(reference, batch_id):
        await read.wait()
        await adopted(reference, batch_id)

    monkeypatch.setattr(journal, "in_batch", paused)
    monkeypatch.setattr(journal, "adopted", after_the_read)
    first = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: "batch_1" in client.made)
    client.end("batch_1")
    second = asyncio.create_task(gw.ask("brief", ask(1)))
    assert (await asyncio.wait_for(first, 3)).usage.service_tier == "batch"
    client.misses = 100  # a lookup now would miss it
    go.set()
    assert (await asyncio.wait_for(second, 3)).usage.service_tier == "batch"
    assert client.listings == 1  # the first task's, finding it
    await gw.close()


async def test_an_item_asking_again_takes_a_stop_on_a_p3_row_its_run_names_it_on(conn: Session, db):
    """A P3 nightly's call names no item, its run does; its batch's refusal was settled on its
    row. The item, asking again, takes that refusal (down to the role's fallback) rather than
    batching the same model again."""
    dead = await started(conn, "nightly", alive=True)
    gw, anthropic, _, _ = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    key = gw.request_key(BRIEF, ask("refuse"))
    start = CallStart(
        run_id=dead, work_id=None, operation="brief", model=BRIEF.model, key=key, via="anthropic"
    )
    (row_id,) = await CallJournal(db).start_many([start], "msgbatch_0")
    await conn.execute(
        "UPDATE inference_calls SET error = 'refused: cyber', responded_at = clock_timestamp() "
        "WHERE id = %s",
        (row_id,),
    )
    with working(a_work_item(await item_of(conn, dead))):
        answer = await asyncio.wait_for(gw.ask("brief", ask("refuse")), 3)
    assert answer.model == OTHER.label and anthropic.batches == {}
    await gw.close()


async def test_a_call_whose_look_fails_takes_the_answer_saved_meanwhile_not_a_normal_call(
    conn: Session, db, monkeypatch
):
    """A job's call without a deadline reads a batch's row while the batch goes by its staged
    reference; meanwhile the batch's task finds it, reads it and saves its answer. The call's
    look, by the batch's id, then fails to read it: the answer is known, so the call takes it
    rather than paying for a normal call."""
    client = OpenAIBatches()  # no normal call can be made (`responses`)
    client.creating = [(True, lost())]
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    journal = CallJournal(db, await started(conn, "job"))
    gate = gw.gate.journal
    assert isinstance(gate, CallJournal)
    in_batch, adopted, read, go = journal.in_batch, gate.adopted, asyncio.Event(), asyncio.Event()

    async def paused(key, work_id, owned=None):
        row = await in_batch(key, work_id, owned)
        if row is not None:
            read.set()
            await go.wait()
        return row

    async def after_the_read(reference, batch_id):
        await read.wait()
        await adopted(reference, batch_id)

    provider = gw.gate.providers["openai"]
    results, failing = provider.results, []

    async def flaky(batch_id):
        if failing:
            failing.pop()
            raise ProviderError("500 Internal Server Error")
        return await results(batch_id)

    monkeypatch.setattr(journal, "in_batch", paused)
    monkeypatch.setattr(gate, "adopted", after_the_read)
    monkeypatch.setattr(provider, "results", flaky)
    first = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: "batch_1" in client.made)
    client.end("batch_1")
    job = asyncio.create_task(Tonight(gw.gate, journal, deadline=None).ask("brief", ask(1)))
    assert (await asyncio.wait_for(first, 3)).usage.service_tier == "batch"
    failing.append(True)  # the job's look fails to read it
    go.set()
    answer = await asyncio.wait_for(job, 3)
    assert answer.usage.reused and answer.usage.cost == 0.0
    assert len(await calls(conn)) == 1  # one row, one charge
    await gw.close()


async def test_a_staged_batch_whose_name_is_slow_to_read_is_looked_for_its_whole_window(
    conn: Session, db, monkeypatch
):
    """A dead run's batch goes by its staged reference; recovery reads its name again, which
    takes longer than the window a batch is looked for in (the database slow, its reads tried
    again): the window starts once the name is read, so the batch is still looked for, found
    and read, not given up on without a lookup."""
    client = OpenAIBatches()
    client.creating = [(True, lost())]
    gw, _, clock = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    gw.batchers["openai"].find_delays = (30.0,)  # the run dies while it looks
    asked = asyncio.create_task(gw.ask("brief", ask(1)))

    async def staged():
        return [c["batch_id"] for c in await calls(conn)] == ["staged:file-1"]

    await until(staged)
    await until(lambda: "batch_1" in client.made)  # made, its reply lost
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()

    journal = CallJournal(db, await started(conn, "nightly"))
    named = journal.named

    async def slow(batch):
        await asyncio.sleep(1.2)  # past FAST's window of a second
        return await named(batch)

    monkeypatch.setattr(journal, "named", slow)
    client.end("batch_1")
    gw, _, _ = openai_batching(journal, client, clock)
    await gw.recover()
    await gw.settle()
    assert [c["cost"] for c in await calls(conn)] == [pytest.approx(SOL_BATCHED)]
    assert client.listings == 1
    await gw.close()


async def test_calls_that_look_once_bring_lookups_forward_and_leave_the_scheduled_ones(
    conn: Session, db
):
    """A batch whose creation lost its reply is looked for on a schedule; three jobs' calls
    without a deadline each wake a lookup of their own, which misses: the scheduled lookups
    still come, and one of them finds the batch."""
    client = OpenAIBatches()
    client.creating, client.misses = [(True, lost())], 4
    unusable = SdkClient(openai_response('{"yes": tr'))  # a job's normal call, cached by none
    client.responses = SimpleNamespace(create=unusable.responses.create)
    gw, _, _ = openai_batching(CallJournal(db, await started(conn, "nightly")), client)
    batcher = gw.batchers["openai"]
    batcher.find_seconds, batcher.find_delays = 10.0, (0.5, 0.2, 0.2)
    asked = asyncio.create_task(gw.ask("brief", ask(1)))
    await until(lambda: "batch_1" in client.made)
    for _ in range(3):
        job = Tonight(gw.gate, CallJournal(db, await started(conn, "job")), deadline=None)
        with pytest.raises(CallError, match="invalid"):
            await asyncio.wait_for(job.ask("brief", ask(1)), 3)
    assert client.listings == 3  # the jobs' own, before any scheduled one
    client.end("batch_1")
    assert (await asyncio.wait_for(asked, 5)).usage.service_tier == "batch"
    assert client.listings == 5
    await gw.close()


async def test_a_batch_whose_deadline_passes_while_its_status_is_read_is_cancelled_at_that_look(
    conn: Session, db, monkeypatch
):
    """The deadline passes while a batch's status is read: it is cancelled at that look, as
    the clock stands then, not a poll later."""
    gw, anthropic, _, clock = batching(CallJournal(db, await started(conn, "nightly")), hold=True)
    gw.batchers["anthropic"].poll = 60.0
    status = anthropic.status

    async def late(batch_id):
        found = await status(batch_id)
        clock.now = DEADLINE  # the read took until the deadline
        return found

    monkeypatch.setattr(anthropic, "status", late)
    asked = asyncio.create_task(gw.ask("brief", ask(0)))
    await until(lambda: anthropic.cancelled == ["msgbatch_1"])
    anthropic.end("msgbatch_1", answered={gw.request_key(BRIEF, ask(0))})
    asked.cancel()
    await asyncio.gather(asked, return_exceptions=True)
    await gw.close()
