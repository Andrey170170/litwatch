"""The Digest tab's routes: a Day, a Week, History, a digest, and writing a failed one again
(docs/specs/v1/DESIGN.md, Digest; digests.md, what the Digest tab shows)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request

from litwatch.api import schemas as s
from litwatch.api.deps import Conn
from litwatch.api.reading import _looks
from litwatch.core.digests import DigestPeriod, week_of
from litwatch.jobs import digests as jobs
from litwatch.jobs.lock import BusyError
from litwatch.store import digests as store_
from litwatch.store import taxonomy as taxonomy_store

router = APIRouter(prefix="/api/digests")

NO_DIGEST = {404: {"description": "No such digest, or its topic is no longer shown"}}
WRITE_AGAIN = NO_DIGEST | {
    409: {"description": "The digest is not failed, its topic is not active, or a job runs"}
}


def _paper(row: dict[str, Any]) -> s.DigestPaper:
    return s.DigestPaper(id=row["paper_id"], revision=row["paper_revision"], **row)


async def _shown(c: psycopg.AsyncConnection) -> list[str]:
    """The shown topics, whose digests the tab shows and steps through."""
    return list(_looks(await taxonomy_store.load(c)))


async def _marks(
    c: psycopg.AsyncConnection, period: DigestPeriod, first: date, last: date
) -> list[tuple[dict[str, Any], s.Badge]]:
    """The digests of `period` from `first` to `last`, of shown topics only, newest day first
    and each day's in the fixed topic order, with their topic's badge."""
    looks = _looks(await taxonomy_store.load(c))
    order = {slug: i for i, slug in enumerate(looks)}
    rows = [r for r in await store_.digests_on(c, period, first, last) if r["topic"] in looks]
    rows.sort(key=lambda r: (-r["day"].toordinal(), order[r["topic"]]))
    return [(r, looks[r["topic"]]) for r in rows]


def _mark(row: dict[str, Any], badge: s.Badge) -> dict[str, Any]:
    return {
        "id": row["id"],
        "topic": badge,
        "period": row["period"],
        "day": row["day"],
        "state": row["state"],
        "counts": s.DigestCounts(**row["counts"]),
        "best_rarity": row["best_rarity"],
    }


async def _cards(c: psycopg.AsyncConnection, period: DigestPeriod, day: date) -> list[s.DigestCard]:
    found = await _marks(c, period, day, day)
    shown: dict[int, list[s.DigestPaper]] = {r["id"]: [] for r, _ in found}
    for p in await store_.card_papers(c, list(shown)):
        shown[p["digest_id"]].append(_paper(p))
    return [
        s.DigestCard(
            **_mark(r, badge),
            gist=r["gist"],
            papers=shown[r["id"]],
            more=r["counts"].get("papers", 0) - len(shown[r["id"]]),
        )
        for r, badge in found
    ]


@router.get("/days/{day}")
async def day(day: date, c: Conn) -> s.Day:
    """A day's daily digests, every topic's at Gist level, and where ‹ › step."""
    weekly = None
    shown = await _shown(c)
    monday = day - timedelta(days=7)
    if day.weekday() == 0 and await _marks(c, DigestPeriod.WEEK, monday, monday):
        weekly = monday
    return s.Day(
        day=day,
        released=await store_.released(c, day),
        digests=await _cards(c, DigestPeriod.DAY, day),
        previous=await store_.step(c, DigestPeriod.DAY, day, False, shown),
        next=await store_.step(c, DigestPeriod.DAY, day, True, shown),
        weekly=weekly,
    )


@router.get("/weeks/{day}")
async def week(day: date, c: Conn) -> s.Week:
    """The weekly digests of the week holding `day`, and where ‹ › step."""
    monday = week_of(day)
    shown = await _shown(c)
    return s.Week(
        monday=monday,
        digests=await _cards(c, DigestPeriod.WEEK, monday),
        previous=await store_.step(c, DigestPeriod.WEEK, monday, False, shown),
        next=await store_.step(c, DigestPeriod.WEEK, monday, True, shown),
    )


@router.get("")
async def history(params: Annotated[s.HistoryIn, Query()], c: Conn) -> s.History:
    """History from `until` back to `since` (at most `HISTORY_SPAN`): every day with its daily
    Marks and whether arXiv listed papers on it, and the weeks those days fall in with their
    weekly Marks."""
    first, last = params.since, params.until
    days: dict[date, list[s.DigestMark]] = {}
    for r, badge in await _marks(c, DigestPeriod.DAY, first, last):
        days.setdefault(r["day"], []).append(s.DigestMark(**_mark(r, badge)))
    weeks: dict[date, list[s.DigestMark]] = {}
    for r, badge in await _marks(c, DigestPeriod.WEEK, week_of(first), last):
        weeks.setdefault(r["day"], []).append(s.DigestMark(**_mark(r, badge)))
    released = await store_.released_days(c, first, last)
    span = [last - timedelta(days=i) for i in range((last - first).days + 1)]
    mondays = sorted({week_of(d) for d in span}, reverse=True)
    return s.History(
        days=[s.HistoryDay(day=d, released=d in released, marks=days.get(d, [])) for d in span],
        weeks=[s.HistoryWeek(monday=m, marks=weeks.get(m, [])) for m in mondays],
        start=await store_.first_day(c, await _shown(c)),
    )


async def _digest(c: psycopg.AsyncConnection, digest_id: int) -> s.Digest:
    row = await store_.get(c, digest_id)
    looks = _looks(await taxonomy_store.load(c))
    if row is None or row["topic"] not in looks:
        raise HTTPException(status_code=404, detail="no such digest")
    return s.Digest(
        **_mark(row, looks[row["topic"]]),
        gist=row["gist"],
        text=row["text"],
        error=row["error"],
        written_at=row["written_at"],
        papers=[_paper(p) for p in await store_.papers_of(c, digest_id)],
    )


@router.get("/{digest_id}", responses=NO_DIGEST)
async def digest(digest_id: int, c: Conn) -> s.Digest:
    """A digest in full: its text and its papers by role, each as it was shown."""
    return await _digest(c, digest_id)


@router.post("/{digest_id}/write", responses=WRITE_AGAIN)
async def write_again(digest_id: int, request: Request, c: Conn) -> s.Digest:
    """Write a failed digest again now, over the papers due for it now, as a run of its own.
    It may fail again. 409 when it is not failed (written meanwhile), its topic is paused, or
    another job holds the run lock."""
    await _digest(c, digest_id)
    app = request.app.state
    try:
        state = await jobs.again(c, app.config, digest_id, app.gateway)
    except BusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if state is None:
        raise HTTPException(status_code=409, detail="the digest is not one to write again")
    return await _digest(c, digest_id)
