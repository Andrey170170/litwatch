from litwatch.models import Confidence, StageRole, best_tier
from tests.conftest import screen_result, verdict


def test_best_tier_is_the_minimum_and_4_when_member_of_none():
    assert best_tier({"a": verdict(3), "b": verdict(2)}) == 2
    assert best_tier({"a": verdict(4)}) == 4
    assert best_tier({}) == 4


def test_screen_result_merges_stages_per_selection():
    r = screen_result(cot_faithfulness=1, mech_interp=3)
    assert r.best_tier == 1 and r.best[0] == "cot-faithfulness"
    assert r.members() == ["cot-faithfulness", "mech-interp"] and r.members(2) == [
        "cot-faithfulness"
    ]
    assert r.tier_for("unknown") == 4
    # A confirm stage overrides only the selection it judged.
    from litwatch.models import Stage, Usage

    r.stages.append(
        Stage(
            role=StageRole.CONFIRM,
            model="muse",
            output={"cot-faithfulness": verdict(2, Confidence.MEDIUM, "demoted")},
            usage=Usage(model="muse", cost=0.01),
        )
    )
    assert r.verdicts["cot-faithfulness"].tier == 2 and r.verdicts["mech-interp"].tier == 3
    assert r.verdicts_after(StageRole.SCREEN)["cot-faithfulness"].tier == 1
    assert r.confirmed and r.confirmed_slugs() == {"cot-faithfulness"} and r.cost == 0.01
