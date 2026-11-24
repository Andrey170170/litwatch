"""The contracts litwatch shares with the deep service (`core.contracts`, generated from
`deep/src/contracts.ts`): each of a read's names that litwatch words for the owner is worded."""

from typing import get_args

from litwatch.api import mcp_deep
from litwatch.core import deepread
from litwatch.core.contracts import deep as contract


def test_every_name_a_read_uses_is_worded():
    assert set(mcp_deep.KINDS) == set(get_args(contract.SectionKind.__value__))
    assert set(deepread.LIMITATION_BY) == set(get_args(contract.LimitationBy.__value__))


def test_deep_input_writes_the_job_a_paper_s_text_is_sent_as(tmp_path, monkeypatch):
    """`litwatch deep input`, for deep's standalone `run FILE.json`: a LaTeX source with its
    outline, as a submission carries it."""
    from typer.testing import CliRunner  # noqa: PLC0415

    from litwatch import cli  # noqa: PLC0415
    from tests.deep_fixtures import ASTRAL  # noqa: PLC0415

    monkeypatch.chdir(tmp_path)  # every command loads ./.env
    (tmp_path / "paper.tex").write_text(ASTRAL)
    argv = ["deep", "input", "paper.tex", "--id", "2610.00001", "--title", "Probes"]
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    sent = contract.JobInput.model_validate_json(result.output)
    assert isinstance(sent.paper, contract.LatexPaper)
    assert [s.number for s in sent.paper.outline.sections] == [None, "1", "2", None, "A"]
