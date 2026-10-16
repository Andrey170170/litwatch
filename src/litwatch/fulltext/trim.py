"""What a full-text pass sends of a paper's text (pipeline.md, section 6): LaTeX text without its
bibliography, which carries nothing a Brief uses and which the archive inlines as a
`thebibliography` block. PDF text is sent as read: its references have no reliable end, and a
cut that ran past them would take the appendix with it."""

from __future__ import annotations

import re

BIBLIOGRAPHY = re.compile(r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}", re.DOTALL)
APPENDIX = re.compile(r"\\appendix\b.*?(?=\\end\{document\}|\Z)", re.DOTALL)


def without_references(text: str, source: str) -> str:
    """LaTeX text without its bibliography; PDF text, or LaTeX without one, as read."""
    return BIBLIOGRAPHY.sub("", text) if source == "latex" else text


def without_appendix(text: str, source: str) -> str:
    """The text without its bibliography and, for LaTeX, without its appendix (everything from
    `\\appendix` to the end of the document). An evaluation variant (docs/plans/model-spend.md,
    P1), not what passes send."""
    text = without_references(text, source)
    return APPENDIX.sub("", text, count=1) if source == "latex" else text
