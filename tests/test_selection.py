from pathlib import Path

import pytest
import yaml

from litwatch.models import Paper
from litwatch.selection import (
    Selection,
    from_spec,
    load_specs,
    render_selections,
    seeds,
    selections_version,
    slugify,
)
from tests.conftest import FAITH, INTERP


def test_version_tracks_screener_visible_content_only():
    same = FAITH.model_copy(update={"owner": "someone else", "status": "paused"})
    assert same.version == FAITH.version
    edited = FAITH.model_copy(update={"rubric": FAITH.rubric + " Also surveys."})
    assert edited.version != FAITH.version
    assert selections_version([FAITH, INTERP]) == selections_version([INTERP, FAITH])
    assert selections_version([FAITH]) != selections_version([FAITH, INTERP])


def test_rubric_gates_tier_1_and_keywords_are_regexes():
    assert FAITH.max_tier == 1 and INTERP.max_tier == 2
    paper = Paper(id="1", title="On CoT monitoring", abstract="We study chains of thought.")
    assert FAITH.keyword_hit(paper) and not INTERP.keyword_hit(paper)
    with pytest.raises(ValueError, match="not a valid regex"):
        Selection(slug="x", name="X", subject="s" * 20, keywords=["(unclosed"])
    assert from_spec(
        {"name": "Agents & tools", "subject": "s" * 20, "keywords": "a, b\nc"}
    ).keywords == ["a", "b", "c"]


def test_slug_and_blank_rubric():
    assert slugify("CoT faithfulness") == "cot-faithfulness"
    assert from_spec({"name": "X y", "subject": "s" * 20, "rubric": "  "}).rubric is None
    with pytest.raises(ValueError):
        slugify("???")


def test_render_and_seeds(tmp_path: Path):
    text = render_selections([FAITH, INTERP])
    assert "## CoT faithfulness (`cot-faithfulness`)" in text
    assert "Tier 1 rubric: A method" in text and "No tier 1 rubric" in text
    packaged = seeds()
    assert {s.slug for s in packaged} == {"cot-faithfulness", "cot-monitoring", "mech-interp"}
    assert next(s for s in packaged if s.slug == "cot-faithfulness").has_rubric
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(FAITH.model_dump(mode="json")))
    assert load_specs(tmp_path)[0].version == FAITH.version
