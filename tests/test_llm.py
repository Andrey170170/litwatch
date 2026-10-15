"""The gateway: one request through either provider, cost, failures, the journal and reuse."""

import json
import sys

import anthropic
import openai
import pytest
from pydantic import BaseModel

from litwatch import store
from litwatch.config import Models, RoleConfig
from litwatch.llm import CallError, Gateway, ProviderError, Request
from litwatch.llm.anthropic import AnthropicProvider, messages_request
from litwatch.llm.openrouter import OpenRouterProvider
from litwatch.store import runs
from litwatch.store.calls import CallJournal
from tests.fakes import ScriptedProvider, SdkClient, anthropic_message, openrouter_completion


class Answer(BaseModel):
    yes: bool
    why: str


REQUEST = Request(system="rubric", user="paper", output=Answer)
GOOD = json.dumps({"yes": True, "why": "it says so"})
HAIKU = RoleConfig(provider="anthropic", model="claude-haiku-5-5", effort="medium")
OPUS = RoleConfig(provider="anthropic", model="claude-opus-5-5", effort="medium")
MUSE = RoleConfig(provider="openrouter", model="meta/muse-spark-1.3-contributor", effort="high")
# Haiku in a chat role, for the cases that price it.
MODELS = Models(writer=HAIKU)


def gateway(models: Models, anthropic=None, openrouter=None, journal=None, debug=False) -> Gateway:
    providers = {}
    if anthropic is not None:
        providers["anthropic"] = AnthropicProvider(client=anthropic)
    if openrouter is not None:
        providers["openrouter"] = OpenRouterProvider(client=openrouter)
    return Gateway(models, providers, journal, retry_delays=(0.0, 0.0), debug=debug)


def test_anthropic_request_carries_the_schema_effort_and_a_cached_system_prompt():
    sent = messages_request(HAIKU, REQUEST)
    assert sent["messages"] == [{"role": "user", "content": "paper"}]
    assert sent["system"] == [
        {"type": "text", "text": "rubric", "cache_control": {"type": "ephemeral"}}
    ]
    assert sent["output_config"]["effort"] == "medium"
    schema = sent["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False and set(schema["properties"]) == {"yes", "why"}


async def test_the_same_request_runs_through_either_provider_by_configuration_alone():
    anthropic = SdkClient(anthropic_message(GOOD))
    openrouter = SdkClient(openrouter_completion(GOOD))
    for writer in (HAIKU, MUSE):
        gw = gateway(Models(writer=writer), anthropic, openrouter)
        answer = await gw.ask("writer", REQUEST)
        assert answer.value == Answer(yes=True, why="it says so")
        assert answer.model == writer.label
    (to_anthropic,), (to_openrouter,) = anthropic.sent, openrouter.sent
    assert to_anthropic["model"] == "claude-haiku-5-5"
    assert to_openrouter["model"] == "meta/muse-spark-1.3-contributor"
    extra = to_openrouter["extra_body"]
    assert extra["reasoning"] == {"effort": "high"}
    # The model's built-in provider list is exclusive, and endpoints must honour the schema.
    assert extra["provider"] == {"require_parameters": True, "order": ["meta"], "only": ["meta"]}
    assert to_openrouter["response_format"]["json_schema"]["strict"] is True


async def test_cost_is_computed_for_anthropic_and_taken_as_reported_from_openrouter():
    gw = gateway(MODELS, SdkClient(anthropic_message(GOOD)), SdkClient(openrouter_completion(GOOD)))
    haiku = (await gw.ask("writer", REQUEST)).usage
    assert (haiku.prompt_tokens, haiku.completion_tokens, haiku.cached_tokens) == (3400, 200, 3000)
    # 400 fresh and 3000 cached input tokens at $0.10 and $0.01, 200 output tokens at $0.50.
    assert haiku.cost == pytest.approx((400 * 0.10 + 3000 * 0.01 + 200 * 0.50) / 1e6)
    opus = (await gw.ask("judge", REQUEST)).usage
    assert opus.cost == pytest.approx((400 * 4.0 + 3000 * 0.2 + 200 * 20.0) / 1e6)
    muse = (await gw.ask("check", REQUEST)).usage
    assert (muse.cost, muse.provider, muse.reasoning_tokens) == (0.0021, "Meta", 300)


async def test_haiku_charges_a_long_prompt_five_times_and_an_unlisted_model_has_no_cost():
    long = SdkClient(anthropic_message(GOOD, fresh=1000, read=100_000, write=2000, output=100))
    usage = (await gateway(MODELS, long).ask("writer", REQUEST)).usage
    assert usage.cost == pytest.approx(
        5 * ((1000 + 100_000 * 0.1 + 2000 * 1.25) * 0.10 + 100 * 0.50) / 1e6
    )
    unlisted = Models(writer=RoleConfig(provider="anthropic", model="claude-next"))
    assert (await gateway(unlisted, long).ask("writer", REQUEST)).usage.cost is None


@pytest.mark.parametrize(
    ("reply", "kind", "detail"),
    [
        (anthropic_message(GOOD, stop_reason="max_tokens"), "truncated", None),
        (anthropic_message("", stop_reason="refusal", category="cyber"), "refused", "cyber"),
        (anthropic_message('{"yes": "maybe"}'), "invalid", "2 validation errors: why, yes"),
    ],
)
async def test_an_unusable_reply_is_an_error_that_says_which_kind(reply, kind, detail):
    with pytest.raises(CallError) as raised:
        await gateway(MODELS, SdkClient(reply)).ask("writer", REQUEST)
    assert (raised.value.kind, raised.value.detail) == (kind, detail)


async def test_openrouter_refusals_and_filtered_replies_are_refusals():
    for reply in (
        openrouter_completion("", refusal="I cannot"),
        openrouter_completion("", finish_reason="content_filter"),
    ):
        with pytest.raises(CallError, match="refused"):
            await gateway(MODELS, openrouter=SdkClient(reply)).ask("check", REQUEST)


async def test_a_briefly_absent_provider_is_tried_again_and_a_timeout_is_not():
    flaky = ScriptedProvider("anthropic", lambda role, request: GOOD)
    failures = [ProviderError("RateLimitError", transient=True)] * 2

    def respond(role, request):
        if failures:
            raise failures.pop()
        return GOOD

    flaky.respond = respond
    gw = Gateway(MODELS, {"anthropic": flaky}, retry_delays=(0.0, 0.0))
    assert (await gw.ask("writer", REQUEST)).value.yes
    assert len(flaky.calls) == 3

    def slow(role, request):
        raise ProviderError("APITimeoutError", timeout=True)

    timing_out = ScriptedProvider("anthropic", slow)
    with pytest.raises(CallError, match="timeout"):
        await Gateway(MODELS, {"anthropic": timing_out}).ask("writer", REQUEST)
    assert len(timing_out.calls) == 1

    def broken(role, request):
        raise ProviderError("BadRequestError: no")

    with pytest.raises(CallError, match="provider: BadRequestError"):
        await Gateway(MODELS, {"anthropic": ScriptedProvider("anthropic", broken)}).ask(
            "writer", REQUEST
        )


def _timeout(sdk, phase: str) -> Exception:
    """The SDK's timeout error as it raises one: from its own HTTP client's, whichever library
    the installed SDK is built on."""
    http = sys.modules[type(sdk.DefaultAsyncHttpxClient()).__mro__[1].__module__.split(".")[0]]
    request = http.Request("POST", "https://api.example.org")
    exc = sdk.APITimeoutError(request=request)
    exc.__cause__ = getattr(http, phase)("timed out", request=request)
    return exc


@pytest.mark.parametrize(
    ("sdk", "provider", "reply", "role"),
    [
        (anthropic, "anthropic", anthropic_message(GOOD), "writer"),
        (openai, "openrouter", openrouter_completion(GOOD), "check"),
    ],
)
async def test_a_connection_that_never_opened_is_tried_again_and_a_read_timeout_is_not(
    sdk, provider, reply, role
):
    client = SdkClient(_timeout(sdk, "ConnectTimeout"), reply)
    assert (await gateway(MODELS, **{provider: client}).ask(role, REQUEST)).value.yes
    assert len(client.sent) == 2
    client = SdkClient(_timeout(sdk, "ReadTimeout"), reply)
    with pytest.raises(CallError, match="timeout: APITimeoutError"):
        await gateway(MODELS, **{provider: client}).ask(role, REQUEST)
    assert len(client.sent) == 1


async def test_a_role_without_credentials_fails_as_a_call_error():
    with pytest.raises(CallError, match="no credentials for openrouter"):
        await gateway(MODELS, SdkClient(anthropic_message(GOOD))).ask("check", REQUEST)


@pytest.fixture
async def journal(conn):
    run_id = await runs.start(conn, "test")
    async with await store.connect(conn.test_dsn, conn.test_schema) as own:
        yield CallJournal(own, run_id)


async def calls(conn):
    return await (await conn.execute("SELECT * FROM inference_calls ORDER BY id")).fetchall()


async def test_calls_are_journalled_and_an_identical_call_is_reused_for_free(conn, journal):
    client = SdkClient(anthropic_message(GOOD))
    gw = gateway(MODELS, client, journal=journal)
    first = await gw.ask("writer", REQUEST, paper_id="2609.1")
    again = await gw.ask("writer", REQUEST, paper_id="2609.1")
    assert len(client.sent) == 1
    assert again.value == first.value and again.usage.reused and again.usage.cost == 0.0
    assert again.usage.original_cost == first.usage.cost
    (row,) = await calls(conn)
    assert (row["operation"], row["paper_id"], row["error"]) == ("writer", "2609.1", None)
    assert row["output"] == {"yes": True, "why": "it says so"}
    assert await journal.total() == pytest.approx(first.usage.cost)


async def test_reuse_is_keyed_by_who_answers_so_judge_and_check_never_share_a_reply(conn, journal):
    said = []

    def respond(role, request):
        said.append(role.label)
        return GOOD

    providers = {n: ScriptedProvider(n, respond) for n in ("anthropic", "openrouter")}
    gw = Gateway(Models(), providers, journal)
    await gw.ask("judge", REQUEST)
    await gw.ask("check", REQUEST)
    # The writer is configured exactly like the judge, so it may share its reply.
    assert (await gw.ask("writer", REQUEST)).usage.reused
    # A replay after a model, effort or routing change calls the new model.
    for changed in (
        OPUS.model_copy(update={"effort": "high"}),
        RoleConfig(provider="anthropic", model="claude-sonnet-5-5", effort="medium"),
    ):
        await Gateway(Models(judge=changed), providers, journal).ask("judge", REQUEST)
    rerouted = MUSE.model_copy(update={"routing": ("other",)})
    await Gateway(Models(check=rerouted), providers, journal).ask("check", REQUEST)
    assert len(said) == 5 and len(set(said)) == 4


async def test_a_rejected_reply_keeps_its_charge_and_is_not_reused(conn, journal):
    client = SdkClient(anthropic_message("", stop_reason="refusal", category="bio"))
    gw = gateway(MODELS, client, journal=journal)
    for _ in range(2):
        with pytest.raises(CallError):
            await gw.ask("writer", REQUEST)
    rows = await calls(conn)
    assert [r["error"] for r in rows] == ["refused: bio", "refused: bio"]
    assert all(r["output"] is None and r["usage"]["cost"] > 0 for r in rows)

    def down(role, request):
        raise ProviderError("APIConnectionError", transient=True)

    offline = Gateway(MODELS, {"anthropic": ScriptedProvider("anthropic", down)}, journal)
    offline.retry_delays = ()
    with pytest.raises(CallError):
        await offline.ask("writer", REQUEST)
    # No cost is invented for a call that never got an answer.
    last = (await calls(conn))[-1]
    assert last["error"] == "provider: APIConnectionError" and last["usage"]["cost"] is None


async def test_a_nul_in_a_reply_is_dropped_so_the_journal_can_store_it(conn, journal):
    client = SdkClient(anthropic_message(json.dumps({"yes": True, "why": "it\x00 says so"})))
    answer = await gateway(MODELS, client, journal=journal).ask("writer", REQUEST)
    assert answer.value.why == "it says so"
    assert (await calls(conn))[0]["output"]["why"] == "it says so"

    refused = SdkClient(anthropic_message("", stop_reason="refusal", category="bio\x00"))
    with pytest.raises(CallError):
        await gateway(MODELS, refused, journal=journal).ask("judge", REQUEST)
    assert (await calls(conn))[-1]["error"] == "refused: bio"
    # The raised error is clean too: its text becomes a paper's stored last error.
    assert str(CallError("provider", "bad\x00 gateway")) == "provider: bad gateway"


INVALID = '{"yes": "maybe"}'


async def test_in_debug_mode_a_failed_reply_is_kept_with_what_the_validator_said(conn, journal):
    gw = gateway(MODELS, SdkClient(anthropic_message(INVALID)), journal=journal, debug=True)
    with pytest.raises(CallError) as raised:
        await gw.ask("writer", REQUEST)
    (row,) = await calls(conn)
    assert row["reply"] == INVALID and row["output"] is None
    assert row["error"] == str(raised.value)
    assert row["error"] == (
        "invalid: 2 validation errors: yes: Input should be a valid boolean, unable to interpret "
        "input; why: Field required"
    )

    cut = '{"yes": true, "why": "it sa'
    truncated = SdkClient(anthropic_message(cut, stop_reason="max_tokens"))
    with pytest.raises(CallError, match="truncated"):
        await gateway(MODELS, truncated, journal=journal, debug=True).ask("judge", REQUEST)
    assert (await calls(conn))[-1]["reply"] == cut


async def test_in_debug_mode_an_openrouter_refusal_keeps_what_the_model_said(conn, journal):
    refused = SdkClient(openrouter_completion("", refusal="I cannot help with that"))
    with pytest.raises(CallError, match="refused: unspecified"):
        await gateway(MODELS, openrouter=refused, journal=journal, debug=True).ask("check", REQUEST)
    assert (await calls(conn))[-1]["reply"] == "I cannot help with that"


async def test_out_of_debug_mode_a_failed_reply_is_not_kept(conn, journal):
    with pytest.raises(CallError):
        await gateway(MODELS, SdkClient(anthropic_message(INVALID)), journal=journal).ask(
            "writer", REQUEST
        )
    (row,) = await calls(conn)
    assert (row["reply"], row["error"]) == (None, "invalid: 2 validation errors: why, yes")


async def test_in_debug_mode_a_kept_failed_reply_is_still_never_reused(conn, journal):
    client = SdkClient(anthropic_message(INVALID), anthropic_message(GOOD))
    gw = gateway(MODELS, client, journal=journal, debug=True)
    with pytest.raises(CallError):
        await gw.ask("writer", REQUEST)
    answer = await gw.ask("writer", REQUEST)
    assert not answer.usage.reused and len(client.sent) == 2
    assert (await gw.ask("writer", REQUEST)).usage.source_call_id == answer.usage.source_call_id


def test_debug_mode_is_off_unless_the_file_or_the_environment_turns_it_on(tmp_path, monkeypatch):
    from litwatch import config  # noqa: PLC0415
    from litwatch.jobs.context import default_gateway  # noqa: PLC0415

    monkeypatch.delenv("LITWATCH_DEBUG", raising=False)
    plain, on = tmp_path / "plain.toml", tmp_path / "debug.toml"
    plain.write_text("")
    on.write_text("debug = true\n")
    assert not config.load(plain).debug and config.load(on).debug
    monkeypatch.setenv("LITWATCH_DEBUG", "0")
    assert not config.load(plain).debug
    monkeypatch.setenv("LITWATCH_DEBUG", "1")
    assert config.load(plain).debug
    monkeypatch.delenv("LITWATCH_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no litwatch.toml here: the defaults, and the environment
    assert config.load().debug
    assert default_gateway(config.load(plain), None).debug
    assert not default_gateway(config.Config(), None).debug


def test_the_sdk_clients_leave_retrying_to_the_gateway():
    assert AnthropicProvider(api_key="k").client.max_retries == 0
    assert OpenRouterProvider(api_key="k").client.max_retries == 0
