import pytest

from litwatch.models import Confidence, Paper, ScreenOutput, StageRole, Tier, Usage
from litwatch.screen import Cascade, keyword_hit, load_system_prompt, render_user_prompt


def out(
    cot: bool, internals: bool = False, confidence: Confidence = Confidence.HIGH
) -> ScreenOutput:
    return ScreenOutput(
        is_cot_monitoring_or_faithfulness=cot,
        uses_internals=internals,
        is_mech_interp=False,
        proposes_definition_or_benchmark=False,
        confidence=confidence,
        reason="r",
    )


class Fake:
    def __init__(self, model: str, output: ScreenOutput) -> None:
        self.model = model
        self.output = output
        self.calls = 0

    async def screen(self, paper: Paper) -> tuple[ScreenOutput, Usage]:
        self.calls += 1
        return self.output, Usage(model=self.model, cost=0.01)


PLAIN = Paper(id="1", title="A survey of graph databases", abstract="We survey storage engines.")
COT = Paper(id="2", title="On CoT monitoring", abstract="We study chain-of-thought monitors.")


def test_keyword_hit():
    assert keyword_hit(COT)
    assert not keyword_hit(PLAIN)


def test_prompt_contains_field_names():
    system = load_system_prompt()
    for name in ScreenOutput.model_fields:
        assert name in system
    assert COT.title in render_user_prompt(COT)


@pytest.mark.parametrize(
    ("paper", "first", "expect_rescreen"),
    [
        (PLAIN, out(False), False),  # confident negative, no keywords
        (PLAIN, out(False, confidence=Confidence.LOW), True),  # low confidence always re-screens
        (COT, out(False), True),  # negative with keyword hit re-screens
        (COT, out(True), False),  # confident positive stays
    ],
)
async def test_rescreen_rule(paper, first, expect_rescreen):
    primary, secondary = Fake("luna", first), Fake("sol", out(False))
    result = await Cascade(primary, secondary).screen(paper)
    assert result.rescreened is expect_rescreen
    assert secondary.calls == (1 if expect_rescreen else 0)
    assert result.model == ("sol" if expect_rescreen else "luna")
    assert result.stages[0].output == first
    assert result.cost == (0.02 if expect_rescreen else 0.01)


async def test_confirm_runs_only_on_routed_tiers_and_wins():
    confirm = Fake("muse", out(True, internals=False))  # demotes to aware
    cascade = Cascade(Fake("luna", out(True, internals=True)), None, confirm)
    result = await cascade.screen(COT)
    assert confirm.calls == 1 and result.confirmed
    assert result.output_after(StageRole.SCREEN).tier == Tier.MUST_READ
    assert result.output_after(StageRole.RESCREEN).tier == Tier.MUST_READ  # no rescreen ran
    assert result.tier == Tier.AWARE

    confirm.calls = 0
    negative = await Cascade(Fake("luna", out(False)), None, confirm).screen(PLAIN)
    assert confirm.calls == 0 and not negative.confirmed and negative.tier == Tier.NONE


async def test_confirm_existing_appends_stage():
    cascade = Cascade(Fake("luna", out(True, internals=True)), None, None)
    prior = await cascade.screen(COT)
    cascade.confirm = Fake("muse", out(False))
    result = await cascade.confirm_existing(COT, prior)
    assert [s.role for s in result.stages] == [StageRole.SCREEN, StageRole.CONFIRM]
    assert prior.tier == Tier.MUST_READ and result.tier == Tier.NONE
