"""What the deep service's tests read of litwatch's side of their boundary, written by litwatch's
own code so deep is tested on what litwatch sends (docs/plans/a2-work-gate.md, D3):

- `deep/tests/fixtures/outlines/<key>.json`: a LaTeX source a deep test reads, and its outline
  as a submission carries it (`integrations.deep.outline`, offsets in UTF-16 code units). The
  key is the first 16 hex digits of the body's SHA-256. deep's `latex(body)` (tests/support.ts)
  writes a missing one by running this module, the body on stdin; CI's deep job has no Python,
  so a new one is committed with the test that needs it.
- `deep/tests/fixtures/job-latex.json`, `job-pdf.json`: whole submissions (`job_input`), one of
  each paper variant, which deep's validator must take (`JOBS`).

`test_deep_fixtures.py` fails when a committed fixture is not what this code writes today;
`uv run python -m tests.deep_fixtures --rewrite` rewrites them all from their bodies.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from litwatch.archive import PaperText
from litwatch.core import Paper
from litwatch.core.contracts import deep as contract
from litwatch.integrations.deep import job_input, outline

FIXTURES = Path(__file__).resolve().parent.parent / "deep" / "tests" / "fixtures"
OUTLINES = FIXTURES / "outlines"

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


def key(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def outline_fixture(body: str) -> dict[str, Any]:
    return {"body": body, "outline": outline(body).model_dump(mode="json", exclude_none=True)}


def job_fixture(name: str) -> dict[str, Any]:
    text, extensions = JOBS[name]
    sent = job_input(PAPER, text, [SELECTION], extensions)
    return sent.model_dump(mode="json", exclude_none=True)


def _write(path: Path, data: dict[str, Any]) -> None:
    """Whole or not at all: deep's test files may write the same fixture at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(f".{id(data)}.part")
    part.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    part.replace(path)


def main(args: list[str]) -> None:
    if args == ["--rewrite"]:
        for path in OUTLINES.glob("*.json"):
            _write(path, outline_fixture(json.loads(path.read_text())["body"]))
        _write(OUTLINES / f"{key(ASTRAL)}.json", outline_fixture(ASTRAL))
        for name in JOBS:
            _write(FIXTURES / name, job_fixture(name))
        return
    body = sys.stdin.read()
    _write(OUTLINES / f"{key(body)}.json", outline_fixture(body))


if __name__ == "__main__":
    main(sys.argv[1:])
