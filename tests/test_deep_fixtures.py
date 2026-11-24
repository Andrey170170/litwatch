"""The fixtures deep's tests read of litwatch's side (`tests/deep_fixtures.py`) are what
litwatch's code writes today, so deep is tested on what it is sent."""

import json

from tests.deep_fixtures import ASTRAL, FIXTURES, JOBS, job_fixture

REWRITE = "run `uv run python -m tests.deep_fixtures --rewrite` and commit what it writes"


def test_each_job_fixture_is_the_submission_litwatch_sends():
    for name in JOBS:
        assert json.loads((FIXTURES / name).read_text()) == job_fixture(name), f"{name}: {REWRITE}"


def test_an_outline_counts_offsets_as_javascript_does():
    """Each offset deep is sent is in UTF-16 code units: a heading's starts where JavaScript finds
    it."""
    sent = json.loads((FIXTURES / "job-latex.json").read_text())["paper"]["outline"]
    assert json.loads((FIXTURES / "job-latex.json").read_text())["paper"]["body"] == ASTRAL
    units = ASTRAL.encode("utf-16-le")
    at = lambda i: units[2 * i :].decode("utf-16-le")  # noqa: E731
    assert [at(s["start"])[:16] for s in sent["sections"]] == [
        "\\begin{abstract}",
        "\\section{Introdu",
        "\\section{Method}",
        "\\begin{thebiblio",
        "\\section{Details",
    ]
    assert at(sent["references"]["start"]).startswith("\\begin{thebibliography}")
    assert at(sent["references"]["end"]).startswith("\n\\appendix")
    assert at(sent["appendix"]).startswith("\\appendix")
