from litwatch.db import parse_screen, rewritten
from litwatch.models import Paper
from tests.conftest import screen_result


def test_parse_screen_roundtrips_and_ignores_pre_selection_rows():
    r = screen_result(cot_faithfulness=2)
    assert parse_screen(r.model_dump(mode="json")) == r
    legacy = {"paper_id": "x", "prompt_version": "v2", "stages": [{"role": "screen"}]}
    assert parse_screen(legacy) is None and parse_screen(None) is None


ABSTRACT = (
    "We probe whether chain-of-thought explanations reflect the computation a model performs. "
    "Planting hints in prompts, we measure how often models use a hint without disclosing it, "
    "across thirteen tasks and ten open models, and release the benchmark."
)


def test_rewritten_ignores_touch_ups_but_not_new_titles_or_rewrites():
    def new(title="A title", abstract=ABSTRACT):
        return Paper(id="1", title=title, abstract=abstract)

    assert not rewritten("A title", ABSTRACT, new(title="A  title", abstract=ABSTRACT + " "))
    assert not rewritten("A title", ABSTRACT, new(abstract=ABSTRACT.replace("ten", "twelve")))
    assert not rewritten("A title", ABSTRACT, new(title="A title."))
    assert rewritten("A title", ABSTRACT, new(title="A better title"))
    assert rewritten("A title", ABSTRACT, new(abstract="We study sparse autoencoders at scale."))
