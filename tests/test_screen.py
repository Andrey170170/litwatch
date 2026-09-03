import pytest

from litwatch.models import Confidence, Paper, ScreenOutput
from litwatch.screen import Cascade, keyword_hit, load_system_prompt, render_user_prompt


def out(cot: bool, confidence: Confidence = Confidence.HIGH) -> ScreenOutput:
    return ScreenOutput(
        is_cot_monitoring_or_faithfulness=cot,
        uses_internals=False,
        is_mech_interp=False,
        confidence=confidence,
        reason="r",
    )


class Fake:
    def __init__(self, model: str, output: ScreenOutput) -> None:
        self.model = model
        self.output = output
        self.calls = 0

    async def screen(self, paper: Paper) -> ScreenOutput:
        self.calls += 1
        return self.output


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
        (PLAIN, out(False, Confidence.LOW), True),  # low confidence always re-screens
        (COT, out(False), True),  # negative with keyword hit re-screens
        (COT, out(True), False),  # confident positive stays
    ],
)
async def test_cascade_rescreen_rule(paper, first, expect_rescreen):
    primary, secondary = Fake("luna", first), Fake("sol", out(True))
    result = await Cascade(primary, secondary).screen(paper)
    assert result.rescreened is expect_rescreen
    assert secondary.calls == (1 if expect_rescreen else 0)
    assert result.model == ("sol" if expect_rescreen else "luna")
    if expect_rescreen:
        assert result.first_stage == first


async def test_cascade_without_secondary():
    result = await Cascade(Fake("luna", out(False, Confidence.LOW)), None).screen(COT)
    assert not result.rescreened and result.model == "luna"
