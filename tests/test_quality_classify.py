"""The classification harness reads the committed labels into cohorts and scores them apart."""

from pathlib import Path

from litwatch import seeds
from litwatch.quality import classify

LABELS = Path(__file__).parent.parent / "evals" / "labels"
TREE = seeds.taxonomy()


def test_the_labels_load_into_cohorts_with_a_fixed_draw():
    labels = classify.load(LABELS)
    provisional = labels.focus["provisional"]
    assert {k: len(v) for k, v in provisional.items()} == {
        "cot-faithfulness": 39,
        "cot-monitoring": 51,
    }
    assert len(labels.silver) == 165 and len(labels.adjudicated) == 20
    assert len(labels.draw) == classify.DRAW
    assert list(labels.draw) == list(classify.load(LABELS).draw)
    assert set(labels.draw) <= set(labels.papers)


def test_a_focus_class_is_reached_by_classifier_band_or_keyword_in_that_order():
    base = dict.fromkeys((c.slug for c in TREE.classes), 0.0)
    by_band = classify.Result(paper_id="1", probabilities={**base, "circuits": 0.3},
                              primary="robotics")  # fmt: skip
    assert classify.focus_routes(TREE, by_band, "circuits") == (False, True, True)
    by_word = by_band.model_copy(update={"keywords": {"cot-faithfulness": "faithful"}})
    assert classify.focus_routes(TREE, by_word, "cot-faithfulness") == (False, False, True)
    child = classify.Result(paper_id="2", probabilities=base, primary="circuits")
    assert classify.focus_routes(TREE, child, "interpretability") == (True, True, True)


def test_precision_and_recall_say_when_there_is_nothing_to_score():
    assert classify.precision_recall([(True, True), (True, False), (False, True)]) == (0.5, 0.5)
    assert classify.precision_recall([(False, False)]) == (None, None)
