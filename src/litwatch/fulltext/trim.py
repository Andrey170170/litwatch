"""What a full-text pass sends of a paper's text (pipeline.md, section 6): the text as read,
without its bibliography, which carries nothing a Brief uses. LaTeX text has the bibliography
inlined by the archive as a `thebibliography` block; PDF text has it under a "References"
heading, cut up to the next heading or the end."""

from __future__ import annotations

import re

BIBLIOGRAPHY = re.compile(r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}", re.DOTALL)
APPENDIX = re.compile(r"\\appendix\b.*?(?=\\end\{document\}|\Z)", re.DOTALL)
# A PDF's references heading, alone on its line: Markdown from pymupdf4llm ("## References",
# "**References**") or plain text from pypdfium2 ("References", "7 References").
REFERENCES = re.compile(
    r"^[#*\s]*(?:\d+\.?\s+)?(?:references|bibliography)[*\s]*$", re.IGNORECASE | re.MULTILINE
)
# The heading after them: Markdown's, or a section's in plain text ("Appendix A ...",
# "Supplementary Material", "A Proofs", "B.1 Setup", "8 Additional Experiments"), short and
# without the commas of a reference. A wrapped reference that looks like one ends the cut early,
# which only keeps some references: when unsure, the text is kept.
HEADING = re.compile(
    r"^(?:#{1,6}\s+\S|\*\*[^*\n]+\*\*\s*$"
    r"|(?i:appendix|appendices|supplementary|supplemental)\b"
    r"|(?:[A-Z]|\d+)(?:\.\d+)*\.?\s+[A-Z][^,\n]{0,80}$)",
    re.MULTILINE,
)


def without_references(text: str, source: str) -> str:
    """The text without its bibliography; unchanged when none is found."""
    if source == "latex":
        return BIBLIOGRAPHY.sub("", text)
    # The last one: a "References" earlier on is more likely a section that cites.
    heading = next(reversed(list(REFERENCES.finditer(text))), None)
    if heading is None:
        return text
    rest = text[heading.end() :]
    after = HEADING.search(rest)
    return text[: heading.start()] + (rest[after.start() :] if after else "")


def without_appendix(text: str, source: str) -> str:
    """The text without its bibliography and, for LaTeX, without its appendix (everything from
    `\\appendix` to the end of the document). An evaluation variant (docs/plans/model-spend.md,
    P1), not what passes send."""
    text = without_references(text, source)
    return APPENDIX.sub("", text, count=1) if source == "latex" else text
