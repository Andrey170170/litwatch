"""Inference recovery uses real PostgreSQL; provider responses are deterministic fakes."""

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import openai
import pytest
from openai.types.chat import ChatCompletion

from litwatch import calls, db, ingest
from litwatch.calls import CallJournal, current_journal, usage_of
from litwatch.config import Settings
from litwatch.digest import Digester, SelectionOutput
from litwatch.models import Paper
from litwatch.screen import Cascade, ModelScreener
from tests.conftest import FAITH, INTERP, SELECTIONS, verdict


def response(content, cost=0.01):
    return ChatCompletion.model_validate(
        {
            "id": "gen-test",
            "object": "chat.completion",
            "created": 1,
            "model": "test",
            "provider": "OpenAI",
            "service_tier": "flex",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 2000,
                "completion_tokens": 100,
                "total_tokens": 2100,
                "cost": cost,
                "cache_discount": 0.002,
                "prompt_tokens_details": {"cached_tokens": 1200, "cache_write_tokens": 128},
                "completion_tokens_details": {"reasoning_tokens": 60},
            },
        }
    )


class Responses:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    async def create(self, **request):
        self.calls.append(request)
        if len(self.calls) == self.fail_at:
            return response("not valid JSON")
        fields = request["response_format"]["json_schema"]["schema"]["required"]
        import json

        return response(json.dumps({field: verdict(2).model_dump(mode="json") for field in fields}))

    def client(self):
        return SimpleNamespace(chat=SimpleNamespace(completions=self))


def test_usage_retains_provider_cache_reasoning_and_missing_fields():
    usage = usage_of(response("{}"), "requested-model")
    assert usage.model == "requested-model"
    assert usage.provider == "OpenAI" and usage.service_tier == "flex"
    assert usage.cached_tokens == 1200 and usage.cache_write_tokens == 128
    assert usage.reasoning_tokens == 60 and usage.generation_id == "gen-test"
    assert usage.cost == 0.01 and usage.cache_discount == 0.002
    missing = usage_of(SimpleNamespace(usage=None), "model")
    assert missing.cost is None and missing.cached_tokens is None


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_timeout_must_be_bounded_and_positive(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        ModelScreener(None, "m", timeout_seconds=timeout)


async def test_screener_preserves_strict_schema_and_flex_fallback_request():
    responses = Responses()
    screener = ModelScreener(responses.client(), "luna", provider_order=("openai/flex", "openai"))
    await screener.screen(Paper(id="x", title="x", abstract="x"), [FAITH])
    request = responses.calls[0]
    assert request["extra_body"]["provider"] == {
        "require_parameters": True,
        "order": ["openai/flex", "openai"],
        "only": ["openai/flex", "openai"],
    }
    assert request["response_format"]["json_schema"]["strict"] is True


@pytest.fixture
async def conn():
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("needs disposable PostgreSQL")
    async with await db.connect(os.environ["TEST_DATABASE_URL"]) as c:
        schema = "test_calls_" + uuid4().hex
        await c.execute(f'CREATE SCHEMA "{schema}"')
        await c.execute(f'SET search_path TO "{schema}"')
        await db.apply_schema(c)
        try:
            yield c
        finally:
            await c.execute("SET search_path TO public")
            await c.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def journal(conn):
    now = datetime.now(UTC)
    return CallJournal(conn, await db.start_run(conn, "manual", now, now))


PAPER = Paper(id="test-paper", title="A paper", abstract="An abstract")


async def test_failed_later_confirmation_survives_into_next_run_without_double_charging(conn):
    await db.upsert_papers(conn, [PAPER])
    responses = Responses(fail_at=3)

    def cascade():
        # New objects on retry simulate losing all Python process state.
        return Cascade(
            SELECTIONS,
            ModelScreener(responses.client(), "primary"),
            confirm=ModelScreener(responses.client(), "confirm", operation="confirm"),
        )

    first = await ingest.run(conn, Settings("unused"), cascade(), "manual", 1, fetch=False)
    assert first.failed == 1 and first.screened == 0
    assert first.cost == pytest.approx(0.03)  # Includes the invalid final response.
    assert current_journal.get() is None
    second = await ingest.run(conn, Settings("unused"), cascade(), "manual", 1, fetch=False)
    assert second.failed == 0 and second.screened == 1
    assert len(responses.calls) == 4  # Only the failed confirmation was repeated.
    assert second.cost == pytest.approx(0.01)
    row = await (
        await conn.execute("SELECT screen FROM papers WHERE id=%s", (PAPER.id,))
    ).fetchone()
    stages = row["screen"]["stages"]
    assert [s["usage"]["reused"] for s in stages] == [True, True, False]
    assert stages[0]["usage"]["original_cost"] == 0.01
    assert stages[0]["usage"]["source_call_id"] is not None
    rows = await (await conn.execute("SELECT * FROM inference_calls ORDER BY id")).fetchall()
    assert len(rows) == 4 and rows[2]["output"] is None and rows[2]["error"]
    assert rows[0]["usage"]["cached_tokens"] == 1200


@pytest.mark.parametrize(
    "change",
    [
        "abstract",
        "title",
        "categories",
        "rubric",
        "effort",
        "model",
        "cap",
        "provider",
        "order",
        "prompt",
    ],
)
async def test_changed_input_cannot_reuse_prior_success(conn, monkeypatch, change):
    responses = Responses()
    j = await journal(conn)
    token = current_journal.set(j)
    try:
        screen = ModelScreener(responses.client(), "model")
        await screen.screen(PAPER, SELECTIONS)
        await screen.screen(PAPER, SELECTIONS)
        assert len(responses.calls) == 1
        paper, selections = PAPER, SELECTIONS
        kwargs = {}
        if change in ("abstract", "title"):
            paper = PAPER.model_copy(update={change: "changed"})
        elif change == "categories":
            paper = PAPER.model_copy(update={"categories": ["cs.CL"]})
        elif change == "rubric":
            selections = [FAITH.model_copy(update={"rubric": "Different rubric"}), INTERP]
        elif change == "effort":
            kwargs["effort"] = "high"
        elif change == "cap":
            kwargs["max_completion_tokens"] = 8192
        elif change == "provider":
            kwargs["provider_order"] = ("openai",)
        elif change == "order":
            selections = list(reversed(SELECTIONS))
        elif change == "prompt":
            monkeypatch.setattr("litwatch.screen.load_prompt_template", lambda *a: "New prompt")
        changed = ModelScreener(
            responses.client(), "other" if change == "model" else "model", **kwargs
        )
        await changed.screen(paper, selections)
        assert len(responses.calls) == 2
    finally:
        current_journal.reset(token)


async def test_timeout_records_unknown_cost_and_is_not_cached(conn):
    class Slow(Responses):
        async def create(self, **request):
            await asyncio.Event().wait()

    j = await journal(conn)
    token = current_journal.set(j)
    try:
        with pytest.raises(TimeoutError):
            await ModelScreener(Slow().client(), "model", timeout_seconds=0.01).screen(
                PAPER, [FAITH]
            )
        row = await (await conn.execute("SELECT * FROM inference_calls")).fetchone()
        assert row["usage"]["cost"] is None and row["output"] is None
        assert row["error"] == "TimeoutError"
    finally:
        current_journal.reset(token)


async def test_explicit_rescreen_invalidates_successes_without_erasing_charge_history(conn):
    await db.upsert_papers(
        conn,
        [
            PAPER.model_copy(
                update={
                    "published": datetime.now(UTC).isoformat(),
                }
            )
        ],
    )
    responses = Responses()
    cascade = Cascade([FAITH], ModelScreener(responses.client(), "primary"))
    first = await ingest.run(conn, Settings("unused"), cascade, "manual", 1, fetch=False)
    assert first.screened == 1
    assert await db.forget_verdicts(conn, FAITH.slug) == 1
    # Forgotten verdicts are backlog: a run leaves them to the drain.
    assert (
        await ingest.run(conn, Settings("unused"), cascade, "manual", 1, fetch=False)
    ).screened == 0
    second = await ingest.drain_backlog(conn, Settings("unused"), cascade, None, asyncio.Event())
    assert second.screened == 1 and len(responses.calls) == 2
    assert first.cost == second.cost == 0.01
    rows = await (await conn.execute("SELECT * FROM inference_calls ORDER BY id")).fetchall()
    assert rows[0]["invalidated_at"] and rows[0]["output"]
    assert rows[1]["invalidated_at"] is None


async def test_digest_retry_keeps_both_response_charges(conn):
    class Writer(Responses):
        async def create(self, **request):
            self.calls.append(request)
            return response(
                "invalid"
                if len(self.calls) == 1
                else '{"overview":"Summary", "must_read":[], "recommended":[]}'
            )

    j = await journal(conn)
    token = current_journal.set(j)
    try:
        await Digester(Writer().client(), "digest")._ask("system", "input", SelectionOutput)
        assert await j.total() == pytest.approx(0.02)
        rows = await (await conn.execute("SELECT * FROM inference_calls ORDER BY id")).fetchall()
        assert rows[0]["error"] and rows[1]["error"] is None
        assert all(row["output"] is None for row in rows)  # Explicit rewrites remain fresh.
    finally:
        current_journal.reset(token)


async def test_a_provider_that_is_briefly_away_is_tried_again(monkeypatch):
    monkeypatch.setattr(calls, "RETRY_DELAYS", (0, 0))
    gone = openai.NotFoundError(
        "No endpoints found",
        response=httpx.Response(404, request=httpx.Request("POST", "http://openrouter")),
        body=None,
    )

    class Flaky(Responses):
        def __init__(self, failures):
            super().__init__()
            self.failures = failures

        async def create(self, **request):
            if self.failures:
                self.failures -= 1
                raise gone
            return await super().create(**request)

    recovering = Flaky(2)
    await ModelScreener(recovering.client(), "model").screen(PAPER, [FAITH])
    assert recovering.failures == 0 and len(recovering.calls) == 1
    with pytest.raises(openai.NotFoundError):
        await ModelScreener(Flaky(3).client(), "model").screen(PAPER, [FAITH])
