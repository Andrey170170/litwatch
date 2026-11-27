"""The classification harness reads the committed labels into cohorts and scores them apart."""

import gzip
from pathlib import Path

import pytest

from litwatch import seeds
from litwatch.quality import classify

LABELS = Path(__file__).parent.parent / "evals" / "labels"
TREE = seeds.taxonomy()


def test_the_labels_load_into_cohorts_with_a_fixed_draw():
    labels = classify.load(LABELS)
    provisional = labels.focus["provisional"]
    assert {k: len(v) for k, v in provisional.items()} == {
        "cot-faithfulness": 38,
        "cot-monitoring": 48,
    }
    assert len(labels.silver) == 165 and len(labels.adjudicated) == 20
    # Memberships come from every paper both labs sorted, disputed primaries included.
    assert "2607.03502" in labels.memberships and "2607.03502" not in labels.silver
    assert len(labels.memberships) == 198
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


def test_a_failed_paper_reaches_a_focus_class_by_keyword_alone():
    failed = classify.Result(paper_id="1", error="timeout", keywords={"cot-faithfulness": "x"})
    assert classify.focus_routes(TREE, failed, "cot-faithfulness") == (False, False, True)
    assert classify.members(TREE, failed) == set()


def test_a_run_refuses_to_resume_answers_to_another_request(tmp_path):
    from litwatch.config import Models, RoleConfig

    out = tmp_path / "jev.jsonl"
    out.write_text(classify.Result(paper_id="1", request="older").model_dump_json() + "\n")
    role = RoleConfig(provider="openrouter", model="typesafe/jev-1.13", decisions=True)
    models = Models(classify=role)
    with pytest.raises(ValueError, match="another request"):
        classify.left(out, classify.fingerprint(models, TREE), ["1", "2"])
    assert classify.left(out, "older", ["1", "2"]) == ["2"]  # what it holds is not asked again
    # The keyword check is part of the request: new patterns are another request.
    patterns = TREE.model_copy(deep=True)
    patterns.topics[0].keywords = [*patterns.topics[0].keywords, "new pattern"]
    assert classify.fingerprint(models, patterns) != classify.fingerprint(models, TREE)


def test_a_resumed_run_reads_its_archived_answers_and_the_new_ones(tmp_path):
    out = tmp_path / "jev.jsonl"
    older, newer = classify.Result(paper_id="1"), classify.Result(paper_id="2")
    (tmp_path / "jev.jsonl.gz").write_bytes(gzip.compress(older.model_dump_json().encode()))
    out.write_text(newer.model_dump_json() + "\n")
    assert set(classify.results(out)) == {"1", "2"}


def test_a_record_holding_a_line_separator_is_one_record_packed_or_appended(tmp_path):
    """JSON escapes no U+2028 (nor U+0085, U+2029): records split on the LF byte alone."""
    out = tmp_path / "jev.jsonl"
    older = classify.Result(paper_id="1", error="cut off\u0085here")
    newer = classify.Result(paper_id="2", error="and there")
    assert " " in older.model_dump_json()  # written raw, not escaped
    (tmp_path / "jev.jsonl.gz").write_bytes(gzip.compress(older.model_dump_json().encode()))
    out.write_text(newer.model_dump_json() + "\n", encoding="utf-8")
    found = classify.results(out)
    assert set(found) == {"1", "2"} and found["1"].error == older.error
