"""Digests' rules (docs/specs/v1/digests.md): what each depth gets, a digest's states and its
papers' roles, which arXiv listing first showed a paper (the day it belongs to), the weeks, and
the rule checks on a digest's voice."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from litwatch.core.taxonomy import Depth
from litwatch.core.types import Paper


class DigestPeriod(StrEnum):
    DAY = "day"
    WEEK = "week"


class DigestState(StrEnum):
    """A digest is published once written, or once it says "Nothing new"; a failed one is
    written again by the next run, over the papers due then."""

    WRITTEN = "written"
    NOTHING_NEW = "nothing_new"
    FAILED = "failed"


PUBLISHED = (DigestState.WRITTEN, DigestState.NOTHING_NEW)


class DigestRole(StrEnum):
    """How a digest shows one of its papers."""

    PICK = "pick"  # put forward by name
    LISTED = "listed"  # in its list (a Focus digest's papers, an Area's "also due")
    MENTIONED = "mentioned"  # named in its text, not a pick


# The depths with a daily digest; every depth has a weekly one.
DAILY = (Depth.FOCUS, Depth.AREA)
# How many picks a weekly digest makes (fewer when the week had fewer papers).
WEEKLY_PICKS = {Depth.AREA: 5, Depth.FIELD: 3}
# A paper announced longer ago than this before a digest's day never becomes due for it: late
# papers arrive within days (a grading retried, a text awaited up to a week), while a paper the
# collection gains much later (a re-run after an edit, a miss, an old paper read on demand) was
# never news.
DUE_WINDOW = timedelta(days=21)

# arXiv takes submissions until 14:00 US Eastern on weekdays and announces them at 20:00 the
# same evening (Friday's on Sunday evening), in the listing dated the next weekday.
ARXIV_ZONE = ZoneInfo("America/New_York")
CUTOFF = time(14)


def next_weekday(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def listing_day(submitted: datetime | date) -> date:
    """The date of the arXiv listing that first showed a paper submitted then: the weekday
    after the submission deadline it made. A date without a time is taken as before that day's
    deadline. arXiv's holidays are not known, so a paper of one lands on the holiday."""
    if isinstance(submitted, datetime):
        if submitted.tzinfo is None:  # arXiv's times are UTC
            submitted = submitted.replace(tzinfo=UTC)
        local = submitted.astimezone(ARXIV_ZONE)
        day, late = local.date(), local.time() >= CUTOFF
    else:
        day, late = submitted, False
    if day.weekday() >= 5 or late:
        day = next_weekday(day)
    return next_weekday(day)


def announced(paper: Paper) -> date | None:
    """The listing day of the paper's first version: from its submission time where the
    version history has it, else from its first-version date, a time and all where the source
    gives one (arXiv's search does; OAI gives a day, which reads as midnight UTC)."""
    for v in paper.version_history:
        if v.get("version") == 1 and v.get("date"):
            try:
                return listing_day(datetime.fromisoformat(str(v["date"])))
            except ValueError:
                break
    if paper.published:
        at = datetime.fromisoformat(paper.published)
        at = at.replace(tzinfo=at.tzinfo or UTC).astimezone(UTC)
        return listing_day(at.date() if at.time() == time() else at)
    return None


def week_of(day: date) -> date:
    """The Monday of the Monday-to-Sunday week `day` is in."""
    return day - timedelta(days=day.weekday())


# -- the voice -----------------------------------------------------------------------------------

# What digest text must not say (digests.md, writing): words to the reader, advice on reading,
# and litwatch's own judgements by name. Matched case-insensitively outside the papers' titles.
VOICE_RULES: dict[str, str] = {
    "addresses the reader": r"\byou(?:r|rs|rself|'re|'ll)?\b",
    "names an audience": (
        r"\bfor (?:\w+[- ]){0,2}(?:builders|practitioners|researchers|readers|teams|anyone|"
        r"engineers|developers)\b"
    ),
    "advises reading": r"\b(?:required|essential|recommended) reading\b|\bworth (?:a )?read",
    "names a grade": r"\bmust[- ]reads?\b|\brarity\b|\b(?:graded|grades?) (?:as|at)\b",
}


def voice_problems(text: str, titles: Iterable[str] = ()) -> list[str]:
    """The voice rules `text` breaks, each once, by name. The papers' titles, and their parts
    before a colon (the writer shortens a long title so), are taken out first: a title may say
    "you" ("Attention Is All You Need")."""
    text = " ".join(text.split())
    names = {n for t in titles for n in (" ".join(t.split()), t.split(":")[0].strip()) if n}
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.I)
    return [name for name, rule in VOICE_RULES.items() if re.search(rule, text, re.I)]
