from litwatch.db import parse_screen
from litwatch.models import StageRole, Tier

LEGACY = {
    "paper_id": "x",
    "prompt_version": "v2",
    "model": "openai/gpt-5.6-sol",
    "rescreened": True,
    "output": {
        "is_cot_monitoring_or_faithfulness": True,
        "uses_internals": True,
        "is_mech_interp": True,
        "proposes_definition_or_benchmark": False,
        "confidence": "high",
        "reason": "second",
    },
    "first_stage": {
        "is_cot_monitoring_or_faithfulness": False,
        "uses_internals": False,
        "is_mech_interp": False,
        "proposes_definition_or_benchmark": False,
        "confidence": "low",
        "reason": "first",
    },
    "usage": [
        {"model": "openai/gpt-5.6-luna", "cost": 0.001},
        {"model": "openai/gpt-5.6-sol", "cost": 0.004},
    ],
}


def test_parse_legacy_screen_reconstructs_stages():
    r = parse_screen(LEGACY)
    assert [s.role for s in r.stages] == [StageRole.SCREEN, StageRole.RESCREEN]
    assert r.stages[0].model == "openai/gpt-5.6-luna" and r.stages[0].output.reason == "first"
    assert r.model == "openai/gpt-5.6-sol" and r.tier == Tier.MUST_READ
    assert r.output_after(StageRole.SCREEN).tier == Tier.NONE
    assert r.cost == 0.005


def test_parse_current_shape_roundtrips():
    r = parse_screen(LEGACY)
    assert parse_screen(r.model_dump(mode="json")) == r
