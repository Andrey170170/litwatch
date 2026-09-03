from pathlib import Path

from litwatch import evalset
from litwatch.evalset import EvalSet, LabeledPaper
from litwatch.evaluation import PRF, Scored, score_tiers
from litwatch.models import Flags, Paper, Tier


def test_score_tiers_counts_tp_fp_fn():
    pairs = [
        (Tier.MUST_READ, Tier.MUST_READ),
        (Tier.MUST_READ, Tier.AWARE),
        (Tier.NONE, Tier.MUST_READ),
        (Tier.NONE, None),  # errored prediction counts as a miss for the expected tier
    ]
    per = score_tiers(pairs)
    assert per[Tier.MUST_READ] == PRF(tp=1, fp=1, fn=1)
    assert per[Tier.AWARE] == PRF(tp=0, fp=1, fn=0)
    assert per[Tier.NONE] == PRF(tp=0, fp=0, fn=2)
    assert per[Tier.MUST_READ].precision == 0.5 and per[Tier.MUST_READ].recall == 0.5


def test_scored_tiers_with_error():
    item = LabeledPaper(
        paper=Paper(id="x", title="t", abstract="a"),
        expected=Flags(cot=True, internals=True, mech_interp=False),
    )
    s = Scored(item=item, result=None, error="boom")
    assert s.expected_tier == Tier.MUST_READ and s.predicted_tier is None


def test_evalset_roundtrip(tmp_path: Path):
    path = tmp_path / "labeled.yaml"
    es = EvalSet(
        papers=[
            LabeledPaper(
                paper=Paper(id="2305.04388", title="T", abstract="A", categories=["cs.CL"]),
                expected=Flags(cot=True, internals=False, mech_interp=False),
                note="n",
            ),
            LabeledPaper(paper=Paper(id="1", title="U", abstract="B")),
        ]
    )
    evalset.save(path, es)
    loaded = evalset.load(path)
    assert loaded.papers == es.papers
    assert "expected: null" in path.read_text()
