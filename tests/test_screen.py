import pytest

from litwatch.models import Confidence, Paper, StageRole, Usage, Verdict
from litwatch.screen import Cascade, output_model, render_system_prompt, render_user_prompt
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
