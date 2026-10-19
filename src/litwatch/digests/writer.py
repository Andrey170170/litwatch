"""The `writer` role's call (digests.md, writing): one call per digest, which writes its text, its
Gist and, by depth, its picks. It asks and writes nothing.

The answer names papers by the arXiv ids it was given, its picks by id and its text by marks
(`core.digests.MARK`); each request's answer model only takes the ids of that digest's papers
(picks from its candidates), so a pick or a mark that is not one fails like any invalid answer.
The papers a text names besides its picks, and the earlier paper a Focus weekly brings back, are
read from its marks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator, model_validator

from litwatch import writing
from litwatch.core import Depth, Topic
from litwatch.core.digests import WEEKLY_PICKS, marked, plain
from litwatch.llm import Request
from litwatch.store.digests import Due

ROLE = "writer"
# The writer reads at most this many papers, rarest first; a digest still covers the rest. A
# Field topic's week can hold a thousand.
MAX_PAPERS = 150
# The accepted lengths guard against a broken answer; the instructions ask for less.
GIST_WORDS = (20, 120)
TEXT_WORDS = {"focus_day": (15, 220), "area_day": (30, 300), "week": (60, 500)}

VOICE = f"""\
{writing.VOICE}

{writing.READER}

Each paper comes with litwatch's standing for it in this topic (its grade and how close it is to \
the topic) and why; use them to judge what matters, and never write them or any other of \
litwatch's terms (grade, must read, Fit, Weight, rarity, verdict). Name a paper by its title, \
shortened where it is long, never by its arXiv id. Where the text first speaks of one of the \
papers, by its title or by what it does, mark it as [those words](its arXiv id, as given), as in \
"[Attention Is All You Need](1706.03762) replaces recurrence with attention" or "[a study of \
attention heads](1905.09418) finds most can be pruned": the reader sees only the words, which open \
the paper. Mark only the papers you are given, and only in the text; the Gist names papers \
without marks. Otherwise plain prose in paragraphs: no headings, lists or other markdown."""

GIST = (
    "A short paragraph of 40 to 80 words that says what the digest says, read before opening it; "
    "it stands on its own, without its text."
)


def _words(text: str, bounds: tuple[int, int], what: str) -> str:
    text = text.strip()
    n = len(text.split())
    if not bounds[0] <= n <= bounds[1]:
        raise ValueError(f"a {what} of {n} words")
    return text


class Pick(BaseModel):
    paper: str = Field(description="The arXiv id of the paper, as given")
    why: str = Field(description="One sentence on what makes it stand out")


class Written(BaseModel):
    """What every digest's answer holds. A subclass per request sets what it may name. A mark
    in the Gist, which names papers without them, is taken out."""

    text: str
    gist: str = Field(description=GIST)

    text_words: ClassVar[tuple[int, int]] = TEXT_WORDS["week"]
    papers: ClassVar[frozenset[str]] = frozenset()  # the ids its text may mark
    candidates: ClassVar[frozenset[str]] = frozenset()  # the ids it may pick
    picks_range: ClassVar[tuple[int, int]] = (0, 0)

    @field_validator("gist")
    @classmethod
    def _gist(cls, gist: str) -> str:
        return _words(plain(gist), GIST_WORDS, "Gist")

    @model_validator(mode="after")
    def _text(self) -> Written:
        self.text = _words(self.text, self.text_words, "text")
        if not set(marked(self.text)) <= self.papers:
            raise ValueError("a paper named that is not one of the digest's")
        return self


class FocusDay(Written):
    text: str = Field(
        description="What the day brought in the topic: 40 to 120 words, a few sentences"
    )


class Picked(Written):
    """An Area's daily digest, and every weekly digest but a Focus topic's: picks, and the
    papers the text names besides them."""

    text: str = Field(description="The digest's text")
    picks: list[Pick]

    @property
    def mentioned(self) -> list[str]:
        """The papers the text marks that are not picks, in the order it names them."""
        ids = {p.paper for p in self.picks}
        return [m for m in marked(self.text) if m not in ids]

    @model_validator(mode="after")
    def _picked(self) -> Picked:
        ids = [p.paper for p in self.picks]
        if len(set(ids)) != len(ids) or not set(ids) <= self.candidates:
            raise ValueError("a pick that is not one of the candidates, or picked twice")
        low, high = self.picks_range
        if not low <= len(ids) <= high:
            raise ValueError(f"{len(ids)} picks, not {low} to {high}")
        return self


class FocusWeek(Written):
    text: str = Field(description="The week in the topic: 150 to 300 words")

    earlier: ClassVar[frozenset[str]] = frozenset()  # the earlier papers it may bring back

    @property
    def recalled(self) -> str | None:
        """The earlier paper the text brings back, or None."""
        return next((m for m in marked(self.text) if m in self.earlier), None)

    @model_validator(mode="after")
    def _one_back(self) -> FocusWeek:
        if len([m for m in marked(self.text) if m in self.earlier]) > 1:
            raise ValueError("more than one earlier paper brought back")
        return self


def _answer[T: Written](base: type[T], **limits: Any) -> type[T]:
    """`base` taking only this digest's papers. The schema is the base's."""
    return type(base.__name__, (base,), {"__module__": __name__, **limits})


# -- what the writer reads -------------------------------------------------------------------------


def _day(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b %Y}"


def _week(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    return f"{monday.day} {monday:%b} to {sunday.day} {sunday:%b %Y}"


def _paper(d: Due, day: date | None = None) -> str:
    standing = f"grade {d.grade.replace('_', ' ')}" if d.grade else "not graded"
    if d.fit:
        standing += f", {d.fit} to the topic"
    lines = [f"[{d.paper_id}] {d.title}"]
    if d.announced and d.announced != day:
        lines[0] += f" (listed {_day(d.announced)})"
    lines.append(f"  Standing: {standing}" + (f". {d.fit_reason}" if d.fit_reason else ""))
    if d.gist:
        lines.append(f"  Gist: {d.gist}")
    if d.brief:
        lines.append(f"  Brief: {' '.join(d.brief.split())}")  # its paragraphs on one line
    elif d.abstract:  # a Focus paper whose Brief is not written yet
        lines.append(f"  Abstract: {d.abstract}")
    return "\n".join(lines)


def _papers(found: Sequence[Due], day: date | None = None) -> tuple[str, list[Due]]:
    """The papers the writer reads, rarest first, and a line on those left out."""
    shown = list(found[:MAX_PAPERS])
    text = "\n\n".join(_paper(d, day) for d in shown)
    if len(found) > len(shown):
        text += f"\n\n({len(found) - len(shown)} more, less notable, are left out here.)"
    return text, shown


def _topic(topic: Topic, name: str) -> str:
    head = f"Topic: {name}. {topic.subject}"
    if topic.interests:
        head += f"\nWithin it, of most interest: {topic.interests}"
    return head


# -- the requests ----------------------------------------------------------------------------------

FOCUS_DAY = f"""\
You write the daily digest of one narrow research topic for litwatch, which reads every new \
arXiv paper. The digest lists the day's papers itself; you write a few sentences on what the day \
brought: what the central papers do and find, how they relate, and, when there are only related \
papers, that the day brought nothing central and what touched the topic. Answer with the \
structured output only.

{VOICE}"""


def focus_day(topic: Topic, name: str, day: date, found: Sequence[Due]) -> Request:
    central, shown = _papers([d for d in found if d.fit == "central"], day)
    related, also = _papers([d for d in found if d.fit != "central"], day)
    parts = [_topic(topic, name), f"Day: {_day(day)}", "Central papers:\n\n" + (central or "none")]
    if related:
        parts.append("Related papers:\n\n" + related)
    papers = frozenset(d.paper_id for d in shown + also)
    output = _answer(FocusDay, text_words=TEXT_WORDS["focus_day"], papers=papers)
    return Request(system=FOCUS_DAY, user="\n\n".join(parts), output=output)


AREA_DAY = f"""\
You write the daily digest of one research area for litwatch, which reads every new arXiv paper. \
Write a short text, 80 to 180 words, on what happened in the area that day: its threads, naming \
papers where they matter; it may name any of the day's papers. Then pick the one or two papers \
that matter most, from the pick candidates only, each with one sentence why; keep it to one or \
two, and pick none when there are no candidates. Answer with the structured output only.

{VOICE}"""


def area_day(topic: Topic, name: str, day: date, found: Sequence[Due]) -> Request:
    text, shown = _papers(found, day)
    candidates = [d.paper_id for d in shown if d.grade == "must_read"]
    parts = [
        _topic(topic, name),
        f"Day: {_day(day)}",
        f"The day's papers ({len(found)}):\n\n{text}",
        "Pick candidates: " + (", ".join(candidates) if candidates else "none"),
    ]
    output = _answer(
        Picked,
        text_words=TEXT_WORDS["area_day"],
        papers=frozenset(d.paper_id for d in shown),
        candidates=frozenset(candidates),
        picks_range=(1, len(candidates)) if candidates else (0, 0),
    )
    return Request(system=AREA_DAY, user="\n\n".join(parts), output=output)


FOCUS_WEEK = f"""\
You write the weekly digest of one narrow research topic for litwatch, which reads every new \
arXiv paper. The digest lists the week's central papers itself; you write what the week was \
about, 150 to 300 words: the thread through its papers, what moved and what is still open. You \
are given the week's daily digests and papers, and earlier papers of the topic: when one of them \
bears on this week's work and may have been forgotten, bring it back, marked like any paper, \
and say why; bring back one at most. Answer with the structured output only.

{VOICE}"""


def _dailies(texts: Sequence[dict[str, Any]]) -> str:
    """Without their marks: the week's papers come with their ids below, and an id a daily
    marked may be one the weekly is not given."""
    return "\n\n".join(f"{_day(t['day'])}: {plain(t['text'])}" for t in texts) or "none written"


def focus_week(
    topic: Topic,
    name: str,
    monday: date,
    found: Sequence[Due],
    dailies: Sequence[dict[str, Any]],
    earlier: Sequence[Due],
) -> Request:
    text, shown = _papers(found)
    back = "\n".join(
        f"[{e.paper_id}] {e.title}" + (f": {e.gist}" if e.gist else "") for e in earlier
    )
    parts = [
        _topic(topic, name),
        f"Week: {_week(monday)}",
        f"The week's daily digests:\n\n{_dailies(dailies)}",
        f"The week's papers ({len(found)}):\n\n{text}",
        f"Earlier papers of the topic:\n\n{back or 'none'}",
    ]
    ids = frozenset(e.paper_id for e in earlier)
    output = _answer(FocusWeek, papers=frozenset(d.paper_id for d in shown) | ids, earlier=ids)
    return Request(system=FOCUS_WEEK, user="\n\n".join(parts), output=output)


WEEK = f"""\
You write the weekly digest of one research {{scope}} for litwatch, which reads every new arXiv \
paper. Write {{asked}}, 150 to 350 words, naming papers where they matter. Then pick exactly \
{{picks}} papers that matter most from the week's papers, each with one sentence why; the \
papers graded must read are the strongest candidates, but any of the week's papers may be \
picked. Answer with the structured output only.

{VOICE}"""


def week(
    topic: Topic,
    name: str,
    monday: date,
    found: Sequence[Due],
    dailies: Sequence[dict[str, Any]],
) -> Request:
    """An Area's weekly digest (the week's main trends, 5 picks) or a Field's (a high-level
    overview, 3 picks)."""
    text, shown = _papers(found)
    picks = min(WEEKLY_PICKS[topic.depth], len(shown))
    if topic.depth == Depth.AREA:
        system = WEEK.format(scope="area", asked="the week's main trends", picks=picks)
    else:
        system = WEEK.format(scope="field", asked="a high-level overview of the week", picks=picks)
    parts = [_topic(topic, name), f"Week: {_week(monday)}"]
    if topic.depth == Depth.AREA:
        parts.append(f"The week's daily digests:\n\n{_dailies(dailies)}")
    parts.append(f"The week's papers ({len(found)}):\n\n{text}")
    ids = frozenset(d.paper_id for d in shown)
    output = _answer(Picked, papers=ids, candidates=ids, picks_range=(picks, picks))
    return Request(system=system, user="\n\n".join(parts), output=output)
