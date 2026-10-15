"""Decision requests: asked of a chat model with a strict schema or of a Decisions endpoint,
validated the same way, and journalled and reused like any call."""

import json

import pytest

from litwatch.config import Models, RoleConfig
from litwatch.llm import CallError, Gateway
from litwatch.llm.anthropic import AnthropicProvider
from litwatch.llm.openrouter import DECISIONS_URL, OpenRouterProvider
from litwatch.llm.types import Choice, DecisionRequest, Predicate
from tests.fakes import SdkClient, anthropic_message, decisions_reply
from tests.test_llm import calls, journal  # noqa: F401

REQUEST = DecisionRequest(
    "Title: Circuits in a small transformer",
    (
        Predicate("circuits", "Is it about circuits?"),
        Predicate("robotics", "Is it about robots?"),
        Choice(
            "primary_class", "Which class?", (("Circuits", "mechanisms"), ("Robotics", "robots"))
        ),
    ),
)
HAIKU = RoleConfig(provider="anthropic", model="claude-haiku-5-5", effort="low")
JEV = RoleConfig(provider="openrouter", model="typesafe/jev-1.13", decisions=True)
CHAT = {
    "circuits": 0.9,
    "robotics": 0.05,
    "primary_class": {"option": "Circuits", "probability": 0.8},
}


def gateway(role: RoleConfig, client: SdkClient, record=None) -> Gateway:
    provider = AnthropicProvider if role.provider == "anthropic" else OpenRouterProvider
    return Gateway(
        Models(classify=role),
        {role.provider: provider(client=client)},
        record,
        retry_delays=(0.0,),
    )


async def test_a_chat_model_answers_through_a_strict_schema_named_by_the_questions():
    client = SdkClient(anthropic_message(json.dumps(CHAT)))
    answer = await gateway(HAIKU, client).decide("classify", REQUEST)
    assert answer.value.probabilities == {"circuits": 0.9, "robotics": 0.05}
    assert answer.value.choices["primary_class"].option == "Circuits"
    schema = client.sent[0]["output_config"]["format"]["schema"]
    assert list(schema["properties"]) == ["circuits", "robotics", "primary_class"]


async def test_a_decisions_endpoint_is_asked_with_typed_questions_and_answers_the_same_way():
    client = SdkClient(
        decisions_reply({
            "circuits": {"type": "noul", "noul": 0.9},
            "robotics": {"type": "noul", "noul": 0.05},
            "primary_class": {"type": "choice", "choice": "Circuits", "confidence": 0.7,
                              "probabilities": {"Circuits": 0.8, "Robotics": 0.2}},
        })
    )  # fmt: skip
    answer = await gateway(JEV, client).decide("classify", REQUEST)
    sent = client.sent[0]
    assert sent["url"] == DECISIONS_URL and sent["state"] == REQUEST.content
    assert sent["questions"]["robotics"] == {"type": "noul", "instructions": "Is it about robots?"}
    assert sent["questions"]["primary_class"]["criteria"] == {
        "Circuits": "mechanisms",
        "Robotics": "robots",
    }
    assert answer.value.probabilities == {"circuits": 0.9, "robotics": 0.05}
    assert answer.value.choices["primary_class"].probability == 0.8
    assert answer.usage.cost == 0.00005


@pytest.mark.parametrize(
    "change",
    [
        {"robotics": 1.4},  # a probability outside [0, 1]
        {"primary_class": {"option": "Biology", "probability": 0.9}},  # not an option
        {"robotics": None},  # a question left unanswered
    ],
)
async def test_an_answer_that_misses_or_breaks_a_question_is_invalid(change):
    reply = {k: v for k, v in {**CHAT, **change}.items() if v is not None}
    client = SdkClient(anthropic_message(json.dumps(reply)))
    with pytest.raises(CallError) as caught:
        await gateway(HAIKU, client).decide("classify", REQUEST)
    assert caught.value.kind == "invalid"


async def test_a_decisions_reply_missing_an_answer_is_invalid():
    client = SdkClient(decisions_reply({"circuits": {"type": "noul", "noul": 0.9}}))
    with pytest.raises(CallError) as caught:
        await gateway(JEV, client).decide("classify", REQUEST)
    assert caught.value.kind == "invalid"


async def test_a_decision_is_journalled_and_reused(conn, journal):  # noqa: F811
    client = SdkClient(anthropic_message(json.dumps(CHAT)))
    gw = gateway(HAIKU, client, journal)
    first = await gw.decide("classify", REQUEST, paper_id="2609.1")
    again = await gw.decide("classify", REQUEST, paper_id="2609.1")
    assert len(client.sent) == 1 and again.usage.reused
    assert again.value == first.value
    assert len(await calls(conn)) == 1


def test_only_openrouter_serves_a_decisions_endpoint():
    with pytest.raises(ValueError, match="only OpenRouter"):
        RoleConfig(provider="anthropic", model="claude-haiku-5-5", decisions=True)
