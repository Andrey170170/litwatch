"""The fixtures deep's tests read of litwatch's side (`tests/deep_fixtures.py`) are what
litwatch's code writes today, so deep is tested on what it is sent."""

import json

from tests.deep_fixtures import ASTRAL, FIXTURES, JOBS, OUTLINES, job_fixture, key, outline_fixture

REWRITE = "run `uv run python -m tests.deep_fixtures --rewrite` and commit what it writes"


def test_every_outline_fixture_is_the_outline_litwatch_sends_of_its_body():
    paths = sorted(OUTLINES.glob("*.json"))
    assert OUTLINES / f"{key(ASTRAL)}.json" in paths, REWRITE
    for path in paths:
        stored = json.loads(path.read_text())
        assert path.stem == key(stored["body"]), path.name
        assert stored == outline_fixture(stored["body"]), f"{path.name}: {REWRITE}"


def test_each_job_fixture_is_the_submission_litwatch_sends():
    for name in JOBS:
        assert json.loads((FIXTURES / name).read_text()) == job_fixture(name), f"{name}: {REWRITE}"


def test_an_outline_counts_offsets_as_javascript_does():
    """Each offset is in UTF-16 code units: a heading's starts where JavaScript finds it."""
    sent = outline_fixture(ASTRAL)["outline"]
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
