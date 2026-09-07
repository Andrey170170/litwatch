from litwatch.db import parse_screen
from tests.conftest import screen_result


def test_parse_screen_roundtrips_and_ignores_pre_selection_rows():
    r = screen_result(cot_faithfulness=2)
    assert parse_screen(r.model_dump(mode="json")) == r
    legacy = {"paper_id": "x", "prompt_version": "v2", "stages": [{"role": "screen"}]}
    assert parse_screen(legacy) is None and parse_screen(None) is None
