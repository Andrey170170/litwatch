"""The LaTeX outline (`core.outline`), over the papers both of litwatch's parsers were tested on
before D3 (docs/plans/a2-work-gate.md): the numbers LaTeX's defaults give the sections, where
the bibliography and the appendix are, and what is not a heading. MCP's own outline tests are in
`test_mcp_reading.py`; the deep service reads what this gives (`test_deep.py`)."""

import pytest

from litwatch.api import mcp_reading
from litwatch.archive import PaperText
from litwatch.core.outline import latex
from litwatch.integrations import deep

# deep's fixture (deep/tests/support.ts): a commented-out number, a bibliography before its
# appendix, and an appendix.
BODY = r"""\documentclass{article}
\begin{document}
\title{Probes read the trace}
\begin{abstract}
Linear probes on the residual stream predict unfaithful traces.
\end{abstract}
\section{Introduction}
Earlier work left open whether a probe can tell an unfaithful trace from a faithful one.
% an earlier draft reported 99.9\% here
\section{Method}
The authors train a linear probe on layer 12 of Qwen3-8B over 1{,}200 labelled traces.
\section{Results}
The probe reaches 71.3\% accuracy against 50\% for a random direction.
\begin{thebibliography}{9}
\bibitem{turpin} Turpin et al. Language models don't always say what they think. 2023.
\end{thebibliography}
\appendix
\section{Probe training details}
The learning rate is 0.003 and training runs for 40 epochs.
\end{document}
"""
NESTED = BODY.replace(
    "\\section{Results}",
    "\\subsection{Probe training}\nIt trains.\n\\subsection*{Aside}\nAn aside.\n"
    "\\subsection{Data}\nTraces.\n\\section{Results}",
)


def numbered(body: str) -> list[str]:
    return [f"{p.number or '-'} {p.title}" for p in latex(body).parts if p.index > 0]


def test_sections_are_counted_lettered_from_the_appendix_on_and_starred_ones_left_bare():
    assert numbered(NESTED) == [
        "- Abstract",
        "1 Introduction",
        "2 Method",
        "2.1 Probe training",
        "- Aside",
        "2.2 Data",
        "3 Results",
        "- References",
        "A Probe training details",
    ]
    # past the alphabet's 26 a section is still counted; only an appendix's letter runs out
    many = "".join(f"\\section{{Part {i}}}\nText.\n" for i in range(25))
    assert numbered(BODY.replace("\\section{Results}", many + "\\section{Results}"))[-3] == (
        "28 Results"
    )
    letters = "\\appendix\n" + "".join(f"\\section{{Extra {i}}}\nText.\n" for i in range(27))
    assert numbered(BODY.replace("\\appendix", letters))[-3:-1] == ["Z Extra 25", "- Extra 26"]
    # a subsection before any section has no number worth giving
    assert numbered("\\subsection{Lead}\nText.\n\\section{One}\n") == ["- Lead", "1 One"]


@pytest.mark.parametrize(
    "custom",
    [
        "\\setcounter{subsection}{3}",
        "\\addtocounter{section}{1}",
        "\\renewcommand\\thesubsection{\\thesection.\\alph{subsection}}",
        "\\numberwithin{subsection}{section}",
        "\\setcounter{secnumdepth}{1}",
        "\\usepackage{chngcntr}",
        "\\chapter{Background}",
        # deep's checks fixture before D3: the appendix numbered S1, S2, ... by its own counter
        "\\setcounter{section}{0}\\renewcommand{\\thesection}{S\\arabic{section}}",
    ],
)
def test_a_source_that_numbers_its_sections_its_own_way_is_not_guessed_at(custom):
    body = NESTED.replace(
        "\\subsection{Probe training}", f"{custom}\n\\subsection{{Probe training}}"
    )
    assert all(p.number is None for p in latex(body).parts)


@pytest.mark.parametrize(
    "cls",
    [
        "\\documentclass[journal]{IEEEtran}",
        "\\documentclass[aps,prl]{revtex4-2}",
        "\\documentclass{report}",
    ],
)
def test_a_class_that_numbers_its_own_way_leaves_every_section_bare(cls):
    body = NESTED.replace("\\documentclass{article}", cls)
    assert all(p.number is None for p in latex(body).parts)


@pytest.mark.parametrize("other", ["\\setcounter{figure}{3}", "% \\setcounter{subsection}{3}"])
def test_a_counter_of_no_section_and_a_setting_in_a_comment_change_nothing(other):
    body = NESTED.replace(
        "\\subsection{Probe training}", f"{other}\n\\subsection{{Probe training}}"
    )
    assert "2.2 Data" in numbered(body)


def test_the_bibliography_is_its_environment_and_the_appendix_runs_from_its_command():
    found = latex(BODY)
    start, end = found.references or (0, 0)
    assert BODY[start:end] == BODY[
        BODY.index("\\begin{thebibliography}") : BODY.index("\\appendix")
    ].rstrip("\n")
    assert found.appendix == BODY.index("\\appendix")
    assert [p.title for p in found.parts if p.appendix] == ["Probe training details"]
    # the space LaTeX allows after \begin and \end moves neither bound
    spaced = BODY.replace("\\begin{thebibliography}", "\\begin {thebibliography}").replace(
        "\\end{thebibliography}", "\\end {thebibliography}"
    )
    start, end = latex(spaced).references or (0, 0)
    assert spaced[start:end].endswith("\\end {thebibliography}")
    # a bibliography that never closes runs to the end
    unclosed = "\\section{A}\nText.\n\\begin{thebibliography}{9}\n\\bibitem{a} A.\n"
    assert latex(unclosed).references == (unclosed.index("\\begin"), len(unclosed))


def test_a_paper_may_have_no_appendix_or_bibliography_and_a_commented_one_is_none():
    bare = "\\documentclass{article}\\begin{document}\\section{Method}We probe.\\end{document}"
    assert (latex(bare).references, latex(bare).appendix) == (None, None)
    commented = (
        "\\begin{document}\n\\section{Method}\nSee the appendix.\n% \\appendix\n"
        "% \\begin{thebibliography}{9}\n\\section{Results}\nDone.\n\\end{document}"
    )
    assert (latex(commented).references, latex(commented).appendix) == (None, None)
    assert numbered(commented) == ["1 Method", "2 Results"]


def test_a_section_named_appendix_starts_it_and_ieeetran_appendices_too():
    named = "\\begin{document}\n\\section{Method}\nWe probe.\n\\section{Appendix: proofs}\nLemma.\n"
    found = latex(named)
    assert found.appendix == named.index("\\section{Appendix")
    # by its name only: LaTeX letters no section of it
    assert numbered(named) == ["1 Method", "2 Appendix: proofs"]
    ieee = "\\begin{document}\n\\section{Method}\nWe probe.\n\\appendices\n\\section{Proofs}\n"
    assert latex(ieee).appendix == ieee.index("\\appendices")
    assert numbered(ieee) == ["1 Method", "A Proofs"]


def test_depths_follow_the_top_level_found_and_spans_hold_what_is_below():
    found = latex(NESTED).parts
    by_title = {p.title: p for p in found}
    assert [p.depth for p in found] == [0, 0, 0, 0, 1, 1, 1, 0, 0, 0]
    method, results = by_title["Method"], by_title["Results"]
    assert method.end == results.start and NESTED[method.start :].startswith("\\section{Method}")
    assert (
        found[0].title == "Before the first heading" and found[0].end == by_title["Abstract"].start
    )


def test_a_heading_whose_title_runs_past_the_scan_is_kept_and_counted():
    long = "\\section[Short]{" + "a long title " * 170 + "}\nText.\n\\section{Next}\n"
    parts = latex(long).parts
    assert [(p.number, p.title[:10]) for p in parts] == [("1", "a long tit"), ("2", "Next")]
    assert len(parts[0].title) < len(long)  # what the scan read of it


def test_a_title_is_parsed_whole_for_deep_and_cut_only_where_mcp_shows_it():
    """deep finds a section by its whole title (`section(title)`), as it did with its own
    parser; MCP's outline shows at most TITLE_CHARS of it."""
    title = "Bounds on " + "\\emph{the} norm " * 30
    body = f"\\section{{{title}}}\nText.\n"
    (part,) = latex(body).parts
    assert part.title == " ".join(title.split()) and len(part.title) > mcp_reading.TITLE_CHARS
    assert deep.outline(body).sections[0].title == part.title
    (shown,) = mcp_reading.outline(PaperText(body, 1, "latex"))
    assert shown.title == part.title[: mcp_reading.TITLE_CHARS]
