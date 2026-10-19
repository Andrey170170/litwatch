"""The `brief` role's call (pipeline.md, section 6): one read of a paper's full text for its Brief,
with an opening line where the text does not support the abstract, a new Gist and the paper's
Weight. It asks and writes nothing."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, Field, field_validator

from litwatch.analysis import GIST_WORDS
from litwatch.core import Paper, TaxonomyClass, Weight
from litwatch.core.analysis import WEIGHT_DEFINITIONS
from litwatch.fulltext import trim
from litwatch.llm import Request
from litwatch.writing import READER, VOICE

ROLE = "brief"
# The Brief asked for is 120–200 words in one or two paragraphs; as with the Gist, the accepted
# range only guards against a broken answer.
BRIEF_WORDS = (90, 240)
BRIEF_PARAGRAPHS = 2
# A longer text is cut: about 70k tokens of LaTeX text (some 2.2 characters a token), which keeps
# the main body and drops the tail of long appendices, at about $0.30 a paper on Opus 5.5; most
# papers are half that.
MAX_TEXT_CHARS = 160_000
CUT = "\n\n[The text is cut here.]"
OVERSTATES = "The abstract overstates:"

SYSTEM = f"""\
You read the full text of one arXiv paper, with its title and abstract, and write about it for \
litwatch, which reads every new paper in a field. Answer with the structured output only.

{VOICE} Rely on the full text: where it and the abstract differ, the full text is what the \
paper shows.

{READER}

overstatement: check each claim of the abstract against what the full text shows. When the \
text does not support one (a gain that holds only in a narrower setting than claimed, a result \
the experiments do not show, a scope wider than what was tested), write one line saying so, \
beginning "{OVERSTATES}", for example "{OVERSTATES} the gain holds only for 7B models on \
GSM8K." Otherwise null. Most papers get null: a claim put more briefly in the abstract than in \
the text is not overstated, and a limit the abstract itself states is not hidden.

brief: one or two paragraphs, 120 to 200 words in all, a blank line between them. From it the \
reader understands what the work is and why it was done, what it found, with the numbers that \
carry it, and how far its evidence goes, in whatever order makes this paper clearest. A second \
paragraph is for a text that reads better split, not for more detail. State results as the full \
text supports them, never as an overstatement line corrects. Do not start with "This paper" and \
do not repeat the title.

weight: how much the paper matters in its own area, which is given with the paper. Judge it \
against that area alone: what is a landmark in one area can be incremental in another. Judge \
from what the full text shows, and give one sentence why in weight_reason. The levels:
{chr(10).join(f"- {w.value}: {meaning}" for w, meaning in WEIGHT_DEFINITIONS.items())}"""


def _words(text: str, bounds: tuple[int, int], what: str) -> str:
    text = " ".join(text.split())
    low, high = bounds
    if not low <= len(text.split()) <= high:
        raise ValueError(f"a {what} of {len(text.split())} words")
    return text


class Output(BaseModel):
    """The answer's schema. The overstatement line comes first, so the Brief and the Gist after
    it are written knowing what it corrects; the reason comes before the Weight it argues for."""

    overstatement: str | None = Field(
        description=f'One line beginning "{OVERSTATES}", or null when the text supports the '
        "abstract"
    )
    brief: str = Field(description="One or two paragraphs, 120 to 200 words in all")
    gist: str = Field(
        description="What the paper does and its main result, as the full text shows them, in "
        "one or two short sentences of 20 to 30 words in all; 30 words is a hard limit. About "
        "the paper, not any area or topic. Never repeat a claim the overstatement line "
        'corrects. Do not start with "This paper" and do not repeat the title.'
    )
    weight_reason: str = Field(description="One sentence")
    weight: Weight

    @field_validator("overstatement")
    @classmethod
    def _one_line(cls, line: str | None) -> str | None:
        line = " ".join((line or "").split())
        if not line:
            return None
        if not line.startswith(OVERSTATES):
            raise ValueError(f'an overstatement line not beginning "{OVERSTATES}"')
        return line

    @field_validator("brief")
    @classmethod
    def _brief_length(cls, brief: str) -> str:
        """Any line break ends a paragraph; the paragraphs are kept, a blank line between them."""
        paragraphs = [" ".join(p.split()) for p in brief.splitlines() if p.strip()]
        if len(paragraphs) > BRIEF_PARAGRAPHS:
            raise ValueError(f"a Brief of {len(paragraphs)} paragraphs")
        _words(" ".join(paragraphs), BRIEF_WORDS, "Brief")
        return "\n\n".join(paragraphs)

    @field_validator("gist")
    @classmethod
    def _gist_length(cls, gist: str) -> str:
        return _words(gist, GIST_WORDS, "Gist")


def request(
    paper: Paper,
    area: TaxonomyClass | None,
    text: str,
    source: str,
    *,
    prepare: Callable[[str, str], str] = trim.without_references,
) -> Request:
    """The instructions repeat across papers, so they are cached; the area, the abstract and the
    full text go with the paper: the text as `prepare` leaves it (without its bibliography; the
    Brief harness compares others), cut at `MAX_TEXT_CHARS`."""
    text = prepare(text, source)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + CUT
    where = f"{area.name}: {area.description}" if area else "not known; judge it in its field"
    user = (
        f"Area: {where}\n\nTitle: {paper.title}\n\nAbstract:\n{paper.abstract}\n\n"
        f"Full text (from its {'LaTeX source' if source == 'latex' else 'PDF'}):\n{text}"
    )
    return Request(system=SYSTEM, user=user, output=Output)
