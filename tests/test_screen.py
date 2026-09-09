from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai import NOT_GIVEN

from litwatch.models import Confidence, Paper, StageRole, Usage, Verdict
from litwatch.screen import (
    Cascade,
    ModelScreener,
    output_model,
    render_system_prompt,
    render_user_prompt,
)
from litwatch.selection import Selection
from tests.conftest import FAITH, INTERP, SELECTIONS, verdict


class Fake:
    """A screener answering the same tiers for every selection it is asked about."""

    def __init__(self, model: str, tier: int, confidence: Confidence = Confidence.HIGH) -> None:
        self.model, self.tier, self.confidence = model, tier, confidence
        self.calls: list[list[str]] = []

    async def screen(self, paper: Paper, selections: list[Selection]):
        self.calls.append([s.slug for s in selections])
        out = {s.slug: verdict(max(self.tier, s.max_tier), self.confidence) for s in selections}
        return out, Usage(model=self.model, cost=0.01)


PLAIN = Paper(id="1", title="A survey of graph databases", abstract="We survey storage engines.")
COT = Paper(id="2", title="On CoT monitoring", abstract="We study chain-of-thought monitors.")


def test_prompt_and_output_model_follow_the_selections():
    system = render_system_prompt(SELECTIONS)
    assert "CoT faithfulness" in system and "cot-faithfulness" in system
    for name in Verdict.model_fields:
        assert name in system
    assert COT.title in render_user_prompt(COT)
    schema = output_model(SELECTIONS).model_json_schema()
    assert set(schema["required"]) == {"cot_faithfulness", "mech_interp"}


@pytest.mark.parametrize(
    ("paper", "tier", "confidence", "expect_rescreen"),
    [
        (PLAIN, 4, Confidence.HIGH, False),  # confident non-member, no keywords
        (PLAIN, 4, Confidence.LOW, True),  # low confidence always re-screens
        (COT, 4, Confidence.HIGH, True),  # non-member with a keyword hit re-screens
        (COT, 2, Confidence.HIGH, False),  # confident member stays
    ],
)
async def test_rescreen_rule(paper, tier, confidence, expect_rescreen):
    primary, secondary = Fake("luna", tier, confidence), Fake("sol", 4)
    result = await Cascade(SELECTIONS, primary, secondary).screen(paper)
    assert result.rescreened is expect_rescreen
    assert len(secondary.calls) == (1 if expect_rescreen else 0)
    assert result.model == ("sol" if expect_rescreen else "luna")
    assert result.cost == (0.02 if expect_rescreen else 0.01)
    assert result.versions == {FAITH.slug: FAITH.version, INTERP.slug: INTERP.version}


async def test_confirm_runs_per_selection_on_tiers_1_and_2_and_wins():
    confirm = Fake("muse", 3)  # demotes whatever it sees to tier 3
    cascade = Cascade(SELECTIONS, Fake("luna", 1), None, confirm)
    result = await cascade.screen(COT)
    # Tier 1 for faithfulness (has a rubric), tier 2 for mech-interp (no rubric): both confirmed.
    assert confirm.calls == [["cot-faithfulness"], ["mech-interp"]]
    assert [s.role for s in result.stages] == [
        StageRole.SCREEN,
        StageRole.CONFIRM,
        StageRole.CONFIRM,
    ]
    assert result.verdicts_after(StageRole.SCREEN)["cot-faithfulness"].tier == 1
    assert result.verdicts_after(StageRole.SCREEN)["mech-interp"].tier == 2
    assert result.best_tier == 3

    confirm.calls = []
    negative = await Cascade(SELECTIONS, Fake("luna", 4), None, confirm).screen(PLAIN)
    assert confirm.calls == [] and not negative.confirmed and negative.best_tier == 4


async def test_confirm_existing_skips_already_confirmed_selections():
    cascade = Cascade(SELECTIONS, Fake("luna", 1), None, None)
    prior = await cascade.screen(COT)
    cascade.confirm = Fake("muse", 4)
    result = await cascade.confirm_existing(COT, prior)
    assert result.confirmed_slugs() == {"cot-faithfulness", "mech-interp"} and result.best_tier == 4
    cascade.confirm = Fake("muse", 1)
    again = await cascade.confirm_existing(COT, result)
    assert cascade.confirm.calls == [] and again == result


def test_cascade_needs_selections():
    with pytest.raises(ValueError):
        Cascade([], Fake("luna", 4))


@pytest.mark.parametrize("secondary_tier", [1, 4])
async def test_partial_rescreen_preserves_primary_and_confirms_all_remaining_positives(
    secondary_tier,
):
    class MixedPrimary(Fake):
        async def screen(self, paper, selections):
            return {FAITH.slug: verdict(4), INTERP.slug: verdict(2)}, Usage(model=self.model)

    secondary, confirm = Fake("glm", secondary_tier), Fake("muse", 3)
    result = await Cascade(SELECTIONS, MixedPrimary("luna", 4), secondary, confirm).screen(COT)
    assert secondary.calls == [[FAITH.slug]]
    assert set(result.stages[1].output) == {FAITH.slug}
    assert result.verdicts_after(StageRole.SCREEN) == {
        FAITH.slug: verdict(4),
        INTERP.slug: verdict(2),
    }
    assert result.verdicts_after(StageRole.RESCREEN) == {
        FAITH.slug: verdict(secondary_tier),
        INTERP.slug: verdict(2),
    }
    assert confirm.calls == ([[FAITH.slug]] if secondary_tier == 1 else []) + [[INTERP.slug]]
    assert result.verdicts[INTERP.slug].tier == 3
    assert result.verdicts[FAITH.slug].tier == (3 if secondary_tier == 1 else 4)
    assert result.versions == {s.slug: s.version for s in SELECTIONS}


@pytest.mark.parametrize(
    ("title", "tier", "confidence", "escalates"),
    [
        ("A graph benchmark", 4, Confidence.HIGH, False),
        ("A graph benchmark", 4, Confidence.LOW, True),
        ("A graph benchmark", 2, Confidence.LOW, True),
        ("A safety benchmark", 4, Confidence.HIGH, True),
        ("A safety benchmark", 2, Confidence.HIGH, False),
    ],
)
async def test_rescreen_ignores_generic_keyword_but_retains_specific_and_uncertain_cases(
    title, tier, confidence, escalates
):
    selection = FAITH.model_copy(update={"keywords": ["BenchMark", "safety benchmark"]})
    paper = Paper(id="3", title=title, abstract="We evaluate models.")
    secondary = Fake("glm", 4)
    result = await Cascade([selection], Fake("luna", tier, confidence), secondary).screen(paper)
    assert result.rescreened is escalates
    # The ingestion/audit matcher and authored spec remain unchanged by escalation policy.
    assert selection.keyword_hit(paper)
    assert selection.keywords == ["BenchMark", "safety benchmark"]
    assert result.versions == {selection.slug: selection.version}


async def test_rescreen_ignored_keywords_can_be_disabled():
    selection = FAITH.model_copy(update={"keywords": ["benchmark"]})
    paper = Paper(id="3", title="A graph benchmark", abstract="We evaluate models.")
    result = await Cascade(
        [selection], Fake("luna", 4), Fake("glm", 4), rescreen_ignored_keywords=()
    ).screen(paper)
    assert result.rescreened


def test_only_uncertain_selection_is_rescreened_without_keyword_hits():
    cascade = Cascade(SELECTIONS, Fake("luna", 4))
    assert cascade.rescreen_selections(
        PLAIN, {FAITH.slug: verdict(4), INTERP.slug: verdict(3, Confidence.LOW)}
    ) == [INTERP]


@pytest.mark.parametrize("token_cap", [None, 8192])
async def test_model_screener_forwards_effort_and_optional_completion_budget(token_cap):
    parsed = output_model([FAITH])(cot_faithfulness=verdict(1))
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))], usage=None
    )
    parse = AsyncMock(return_value=completion)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse)))
    screener = ModelScreener(client, "z-ai/glm-5.3", "high", max_completion_tokens=token_cap)
    output, usage = await screener.screen(COT, [FAITH])
    request = parse.call_args.kwargs
    assert request["model"] == "z-ai/glm-5.3"
    assert request["extra_body"]["reasoning"] == {"effort": "high"}
    assert request["max_completion_tokens"] == (token_cap if token_cap else NOT_GIVEN)
    assert output == {FAITH.slug: verdict(1)}
    assert usage.model == "z-ai/glm-5.3"
