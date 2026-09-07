from pathlib import Path

from litwatch import evalset
from litwatch.evalset import EvalSet, LabeledPaper
from litwatch.evaluation import PRF, Scored, score_selection, score_tiers
from litwatch.models import Paper, StageRole
from tests.conftest import screen_result


def test_score_tiers_counts_tp_fp_fn():
    pairs = [(1, 1), (1, 2), (4, 1), (4, None)]  # an errored prediction is a miss
    per = score_tiers(pairs)
    assert per[1] == PRF(tp=1, fp=1, fn=1)
    assert per[2] == PRF(tp=0, fp=1, fn=0)
    assert per[4] == PRF(tp=0, fp=0, fn=2)
    assert per[1].precision == 0.5 and per[1].recall == 0.5


def test_score_selection_uses_only_papers_labeled_for_it():
    hit = LabeledPaper(
        paper=Paper(id="a", title="t", abstract="a"), expected={"cot-faithfulness": 1}
    )
    miss = LabeledPaper(
        paper=Paper(id="b", title="t", abstract="a"), expected={"cot-faithfulness": 1}
    )
    other = LabeledPaper(paper=Paper(id="c", title="t", abstract="a"), expected={"mech-interp": 2})
    scored = [
        Scored(item=hit, result=screen_result("a", cot_faithfulness=1, mech_interp=4)),
        Scored(item=miss, result=screen_result("b", cot_faithfulness=3, mech_interp=4)),
        Scored(item=other, result=None, error="boom"),
    ]
    report = score_selection("cot-faithfulness", scored)
    assert [s.item.paper.id for s in report.scored] == ["a", "b"]
    assert [s.item.paper.id for s in report.misses] == ["b"]
    assert report.per_tier[1] == PRF(tp=1, fp=0, fn=1) and report.recommended_recall == 0.5
    assert report.per_stage[StageRole.SCREEN][1].tp == 1
    assert scored[2].predicted("mech-interp") is None


def test_evalset_roundtrip(tmp_path: Path):
    path = tmp_path / "labeled.yaml"
    es = EvalSet(
        papers=[
            LabeledPaper(
                paper=Paper(id="2305.04388", title="T", abstract="A", categories=["cs.CL"]),
                expected={"cot-faithfulness": 1, "mech-interp": 4},
                note="n",
            ),
            LabeledPaper(paper=Paper(id="1", title="U", abstract="B")),
        ]
    )
    evalset.save(path, es)
    loaded = evalset.load(path)
    assert loaded.papers == es.papers
    assert "expected: null" in path.read_text() and "schema_version: 2" in path.read_text()
