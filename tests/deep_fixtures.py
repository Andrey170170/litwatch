"""What the deep service's tests read of litwatch's side of their boundary, written by litwatch's
own code so deep is tested on what litwatch sends (docs/plans/a2-work-gate.md, D3): whole
submissions (`job_input`), one of each paper variant, which deep's validator must take, in
`deep/tests/fixtures/` (`JOBS`). The LaTeX one's body puts characters past the Basic Multilingual
Plane before every heading, so its outline's offsets are counted as JavaScript counts them. deep's
other tests state the small outlines they need; litwatch's parser is tested in test_outline.py.

`test_deep_fixtures.py` fails when a committed fixture is not what this code writes today;
`uv run python -m tests.deep_fixtures --rewrite` rewrites them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from litwatch.archive import PaperText
from litwatch.core import Paper
from litwatch.core.contracts import deep as contract
from litwatch.integrations.deep import job_input

FIXTURES = Path(__file__).resolve().parent.parent / "deep" / "tests" / "fixtures"

# Characters past the Basic Multilingual Plane before every heading and the bibliography, which
# JavaScript counts as two code units each.
ASTRAL = r"""\documentclass{article}
\begin{document}
\begin{abstract}
We read 𝔼[𝑥] off the residual stream 🧠.
\end{abstract}
\section{Introduction}
Probes 🔍 find 𝒪(n) hidden steps.
\section{Method}
The probe trains on 𝔼-labelled traces.
\begin{thebibliography}{9}
\bibitem{a} A. Author. Traces 📄. 2025.
\bibitem{b} B. Author. More traces. 2026.
\end{thebibliography}
\appendix
\section{Details 🧪}
The learning rate is 0.003.
\end{document}
"""
PAPER = Paper(id="2610.00001", title="Probes read the trace", abstract="A.", authors=["A. Author"])
SELECTION = contract.Selection(name="Faithfulness", slug="cot", subject="CoT.", grade="relevant")
JOBS = {
    "job-latex.json": (PaperText(ASTRAL, 2, "latex"), ["faithfulness"]),
    "job-pdf.json": (PaperText("# Probes\n\nWe read 𝔼[𝑥].\n", None, "pdf"), []),
}


def job_fixture(name: str) -> dict[str, Any]:
    text, extensions = JOBS[name]
    sent = job_input(PAPER, text, [SELECTION], extensions)
    return sent.model_dump(mode="json", exclude_none=True)


def main(args: list[str]) -> None:
    if args != ["--rewrite"]:
        sys.exit("usage: python -m tests.deep_fixtures --rewrite")
    for name in JOBS:
        sent = json.dumps(job_fixture(name), ensure_ascii=False, indent=1)
        (FIXTURES / name).write_text(sent + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
