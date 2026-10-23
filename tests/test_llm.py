"""The gateway: one request through either provider, cost, failures, the journal and reuse."""

import json
import os
import sys

import anthropic
import openai
import pytest
from pydantic import BaseModel

from litwatch import store
from litwatch.config import ROLES, Models, RoleConfig
from litwatch.llm import CallError, Gateway, ProviderError, Reply, Request, Stop, Usage
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


def _status(sdk, code: int, body: dict | None = None) -> Exception:
    """The SDK's error for an HTTP status, as it raises one; `body` is the response's `error`."""
    http = sys.modules[type(sdk.DefaultAsyncHttpxClient()).__mro__[1].__module__.split(".")[0]]
    request = http.Request("POST", "https://api.example.org")
    client = sdk.AsyncAnthropic(api_key="k") if sdk is anthropic else sdk.AsyncOpenAI(api_key="k")
    response = http.Response(code, request=request)
    body = None if body is None else {"error": body}
    return client._make_status_error(f"status {code}", body=body, response=response)


@pytest.mark.parametrize("code", [401, 402, 403])
@pytest.mark.parametrize(
    ("sdk", "provider", "role"),
    [(anthropic, "anthropic", "writer"), (openai, "openrouter", "check")],
)
async def test_an_account_error_is_never_retried_and_journalled_as_its_kind(
    conn, journal, sdk, provider, role, code
):
    client = SdkClient(_status(sdk, code), anthropic_message(GOOD))
    gw = gateway(MODELS, **{provider: client}, journal=journal)
    with pytest.raises(CallError) as caught:
        await gw.ask(role, REQUEST, paper_id=None)
    assert caught.value.kind == "account" and not caught.value.unusable
    assert len(client.sent) == 1
    (row,) = await calls(conn)
    assert row["error"].startswith("account: ")
    assert row["usage"]["via"] == provider  # whose account, though no reply came


def _forbidden(metadata: dict | None) -> dict:
    return {"code": 403, "message": "Forbidden"} | ({"metadata": metadata} if metadata else {})


# OpenRouter's 403s about the request's content (its API reference, errors), each with what
# marks it.
DECLINED = {
    "moderation": {"reasons": ["violence"], "flagged_input": "...", "provider_name": "OpenAI"},
    "guardrail": {"error_type": "permission_denied", "patterns": ["ignore all previous"]},
    "content_policy_violation": {"error_type": "content_policy_violation", "provider_code": "S"},
    "refusal": {"error_type": "refusal", "provider_name": "Anthropic"},
}


@pytest.mark.parametrize("category", list(DECLINED))
async def test_an_openrouter_403_about_the_request_is_a_refusal_that_goes_down_the_list(
    conn, journal, category
):
    openrouter = SdkClient(_status(openai, 403, _forbidden(DECLINED[category])))
    anthropic = SdkClient(anthropic_message(GOOD))
    models = Models(writer=MUSE.model_copy(update={"fallbacks": (HAIKU,)}))
    answer = await gateway(models, anthropic, openrouter, journal=journal).ask("writer", REQUEST)
    assert answer.value.yes and (len(openrouter.sent), len(anthropic.sent)) == (1, 1)
    refused, answered = await calls(conn)
    assert refused["error"] == f"refused: {category}" and answered["error"] is None
    assert refused["usage"]["via"] == "openrouter" and refused["usage"]["cost"] == 0.0


@pytest.mark.parametrize(
    "metadata",
    [
        {"limit_source": "openrouter_key_limit", "scope": "key_limit", "limit_usd": 30},
        {"error_type": "permission_denied"},  # no guardrail matched: the key's permission
        None,  # nothing to tell it by
    ],
    ids=["budget", "permission", "unknown"],
)
async def test_any_other_openrouter_403_is_the_account_and_stops_the_list(conn, journal, metadata):
    openrouter = SdkClient(_status(openai, 403, _forbidden(metadata)))
    anthropic = SdkClient(anthropic_message(GOOD))
    models = Models(writer=MUSE.model_copy(update={"fallbacks": (HAIKU,)}))
    with pytest.raises(CallError) as caught:
        await gateway(models, anthropic, openrouter, journal=journal).ask("writer", REQUEST)
    assert caught.value.kind == "account" and anthropic.sent == []
    (row,) = await calls(conn)
    assert row["error"].startswith("account: ")


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


SONNET = RoleConfig(provider="anthropic", model="claude-sonnet-5-5", effort="medium")
FLASH = RoleConfig(provider="openrouter", model="deepseek/deepseek-v4.1-flash-1017", effort="high")
GEMINI = RoleConfig(provider="openrouter", model="google/gemini-3.8-flash", effort="high")
# Opus, then Sonnet from the same lab (D1), then another lab's model.
LISTED = Models(brief=OPUS.model_copy(update={"fallbacks": (SONNET, MUSE)}))


def listed(script: dict[str, object], said: list[str]) -> dict[str, ScriptedProvider]:
    """Providers answering each model as `script` says (a reply's text, a `Reply`, or an
    exception), the answer `GOOD` when it says nothing; `said` gets each model asked."""

    def respond(role, request):
        said.append(role.model)
        answer = script.get(role.model, GOOD)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return {n: ScriptedProvider(n, respond) for n in ("anthropic", "openrouter")}


def refusal(model: str, category: str = "bio") -> Reply:
    return Reply("", Stop.REFUSED, Usage(model=model, cost=0.001), refusal=category)


async def test_a_refusal_goes_down_the_list_in_order_each_journalled_under_the_role(conn, journal):
    said: list[str] = []
    script = {OPUS.model: refusal(OPUS.model), SONNET.model: refusal(SONNET.model, "cyber")}
    gw = Gateway(LISTED, listed(script, said), journal, retry_delays=())
    answer = await gw.ask("brief", REQUEST, paper_id="p")
    assert answer.value.yes and answer.model == MUSE.label
    assert said == [OPUS.model, SONNET.model, MUSE.model]  # in order, in the same run
    rows = await calls(conn)
    assert [(r["operation"], r["usage"]["model"], r["error"]) for r in rows] == [
        ("brief", OPUS.model, "refused: bio"),
        ("brief", SONNET.model, "refused: cyber"),
        ("brief", MUSE.model, None),
    ]
    # Asked again, the answer is reused, and the role starts from the model that gave it.
    again = await gw.ask("brief", REQUEST, paper_id="p")
    assert again.value.yes and again.usage.cost == 0 and again.model == MUSE.label
    assert len(said) == 3
    # Another question starts from the top.
    await gw.ask("brief", Request(system="s", user="other", output=Answer))
    assert said[3] == OPUS.model


async def test_every_model_refusing_stops_at_once_with_the_last_refusal(conn, journal):
    said: list[str] = []
    script = {m.model: refusal(m.model) for m in LISTED.brief.models}
    gw = Gateway(LISTED, listed(script, said), journal, retry_delays=())
    with pytest.raises(CallError) as raised:
        await gw.ask("brief", REQUEST, paper_id="p")
    assert (raised.value.kind, raised.value.model) == ("refused", MUSE.label)
    assert said == [OPUS.model, SONNET.model, MUSE.model]  # each once
    assert [r["error"] for r in await calls(conn)] == ["refused: bio"] * 3


async def test_a_run_started_down_the_list_goes_on_from_the_top_on_refusals():
    """So a stop for every model refusing means every model of the list: the models above
    `start` answered unusably before, not refused."""
    said: list[str] = []
    script = {SONNET.model: refusal(SONNET.model), MUSE.model: refusal(MUSE.model)}
    gw = Gateway(LISTED, listed(script, said), retry_delays=())
    answer = await gw.ask("brief", REQUEST, start=1)
    assert answer.model == OPUS.label
    assert said == [SONNET.model, MUSE.model, OPUS.model]
    said.clear()
    script[OPUS.model] = refusal(OPUS.model)
    with pytest.raises(CallError, match="refused"):
        await gw.ask("brief", Request(system="s", user="all refuse", output=Answer), start=2)
    assert said == [MUSE.model, OPUS.model, SONNET.model]  # each once


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        ("not json", "invalid"),
        (Reply('{"yes": tr', Stop.TRUNCATED, Usage(model="m")), "truncated"),
        (ProviderError("APITimeoutError", timeout=True), "timeout"),
        (ProviderError("InternalServerError", transient=True), "provider"),
    ],
)
async def test_an_unusable_answer_or_none_never_moves_down_in_the_run(answer, kind):
    said: list[str] = []
    gw = Gateway(LISTED, listed({OPUS.model: answer}, said), retry_delays=(0.0,))
    with pytest.raises(CallError) as raised:
        await gw.ask("brief", REQUEST)
    assert (raised.value.kind, raised.value.model) == (kind, OPUS.label)
    assert set(said) == {OPUS.model}  # a provider error is retried, on the same model


async def test_start_asks_further_down_the_list_and_past_its_end_the_last():
    said: list[str] = []
    gw = Gateway(LISTED, listed({}, said), retry_delays=())
    for start, model in ((1, SONNET), (2, MUSE), (7, MUSE)):
        user = f"start {start}"
        answer = await gw.ask("brief", Request(system="s", user=user, output=Answer), start=start)
        assert answer.model == model.label and said[-1] == model.model
    # A list of one: a refusal is the refusal of every model.
    with pytest.raises(CallError, match="refused"):
        await Gateway(
            LISTED, listed({"claude-haiku-5-5": refusal("h")}, said), retry_delays=()
        ).ask("field_grade", REQUEST, start=2)


async def test_a_fallback_without_credentials_is_a_call_with_no_answer():
    said: list[str] = []
    models = Models(brief=OPUS.model_copy(update={"fallbacks": (MUSE, SONNET)}))
    for script, outcome in (({}, None), ({OPUS.model: refusal(OPUS.model)}, "no credentials")):
        anthropic_only = {"anthropic": listed(script, said)["anthropic"]}
        gw = Gateway(models, anthropic_only, retry_delays=())
        if outcome is None:  # what the role's own model answers needs nothing else
            assert (await gw.ask("brief", REQUEST)).value.yes
            continue
        with pytest.raises(CallError, match="no credentials for openrouter"):
            await gw.ask("brief", Request(system="s", user="refused", output=Answer))
    assert said == [OPUS.model, OPUS.model]


async def test_googles_filter_is_a_refusal_and_a_reply_without_choices_a_provider_error():
    """Gemini's filter takes two shapes (evals/2026-10-19-fallback-ladders.md, "For P2"):
    `content_filter`, a refusal, goes down the list; a completion with no choices is the
    provider failing, which never does."""
    models = Models(check=GEMINI.model_copy(update={"fallbacks": (FLASH,)}))
    filtered = SdkClient(
        openrouter_completion("", finish_reason="content_filter"), openrouter_completion(GOOD)
    )
    answer = await gateway(models, openrouter=filtered).ask("check", REQUEST)
    assert answer.model == FLASH.label
    assert [r["model"] for r in filtered.sent] == [GEMINI.model, FLASH.model]
    empty = openrouter_completion(GOOD)
    empty.choices = []
    no_choices = SdkClient(empty)
    with pytest.raises(CallError) as raised:
        await gateway(models, openrouter=no_choices).ask("check", REQUEST)
    assert raised.value.kind == "provider"
    assert {r["model"] for r in no_choices.sent} == {GEMINI.model}


def test_fallbacks_may_share_their_roles_lab_but_judge_and_check_share_none():
    assert Models(judge=OPUS.model_copy(update={"fallbacks": (SONNET,)}))  # D1
    for judge, check in (
        (
            OPUS.model_copy(update={"fallbacks": (MUSE.model_copy(update={"model": "meta/x"}),)}),
            MUSE,
        ),
        (OPUS, MUSE.model_copy(update={"fallbacks": (SONNET,)})),
    ):
        with pytest.raises(ValueError, match="share no lab"):
            Models(judge=judge, check=check)
    assert {m.lab for m in Models().judge.models}.isdisjoint(m.lab for m in Models().check.models)


def test_a_fallback_is_a_plain_chat_model_and_decision_roles_have_none():
    chained = SONNET.model_copy(update={"fallbacks": (MUSE,)})
    with pytest.raises(ValueError, match="without fallbacks of its own"):
        Models(brief=OPUS.model_copy(update={"fallbacks": (chained,)}))
    with pytest.raises(ValueError, match="without fallbacks of its own"):
        Models(
            brief=OPUS.model_copy(
                update={"fallbacks": (MUSE.model_copy(update={"decisions": True}),)}
            )
        )
    with pytest.raises(ValueError, match="listed twice"):
        Models(brief=OPUS.model_copy(update={"fallbacks": (SONNET, SONNET)}))
    decider = Models().classify
    with pytest.raises(ValueError, match="decision role has no fallbacks"):
        Models(classify=decider.model_copy(update={"fallbacks": (MUSE,)}))
    with pytest.raises(ValueError, match="decision role has no fallbacks"):
        Models(area_check=MUSE.model_copy(update={"fallbacks": (FLASH,)}))


def test_the_default_lists_are_the_replays():
    labels = {name: [m.label for m in Models().role(name).models] for name in ROLES}
    assert labels["field_grade"] == ["anthropic:claude-haiku-5-5"]  # no fallback (P1, R1)
    assert labels["brief"] == [
        "anthropic:claude-sonnet-5-5@high",
        "openrouter:openai/gpt-6.1-sol@high",
    ]
    assert len(labels["judge"]) == 4 and len(labels["check"]) == len(labels["analyse"]) == 3
    assert labels["writer"][-1] == "openrouter:moonshotai/kimi-k3@medium"
    assert labels["classify"] == labels["area_check"] == ["openrouter:typesafe/jev-1.13"]


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


@pytest.mark.parametrize(
    "args",
    [
        ["nightly"],
        ["intake"],  # a group's own command
        ["digests", "write", "--since", "2026-10-01", "--until", "2026-10-02"],
        ["taxonomy", "check"],  # reads no configuration, so no helper loaded .env for it
    ],
)
def test_every_command_sees_dotenv_on_entry(tmp_path, monkeypatch, args):
    from typer.testing import CliRunner  # noqa: PLC0415

    from litwatch import cli  # noqa: PLC0415

    monkeypatch.setenv("LITWATCH_DEBUG", "")  # recorded, so the undo removes what .env sets
    monkeypatch.delenv("LITWATCH_DEBUG")
    monkeypatch.delenv("LITWATCH_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LITWATCH_DEBUG=1\n")
    loads, seen, real = [], [], cli.configuration.load_dotenv

    def load_dotenv():
        loads.append(1)
        real()

    def entered(*args):  # the command's first read, then no further: no database, no file
        seen.append(os.environ.get("LITWATCH_DEBUG"))
        raise SystemExit(0)

    monkeypatch.setattr(cli.configuration, "load_dotenv", load_dotenv)
    monkeypatch.setattr(cli.configuration, "load", entered)
    monkeypatch.setattr(cli.seeds, "taxonomy", entered)
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert (seen, len(loads)) == (["1"], 1)  # loaded by the root callback, before the command


def test_the_sdk_clients_leave_retrying_to_the_gateway():
    assert AnthropicProvider(api_key="k").client.max_retries == 0
    assert OpenRouterProvider(api_key="k").client.max_retries == 0
