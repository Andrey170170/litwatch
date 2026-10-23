"""The Digest tab's routes: a Day, a Week, History, a digest, and writing a failed one again as
a job (docs/specs/v1/DESIGN.md, Digest and Jobs; digests.md, what the Digest tab shows)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

import psycopg
from fastapi import HTTPException, Query, Request

from litwatch.api import schemas as s
from litwatch.api.deps import Conn, protected
from litwatch.api.reading import _looks
from litwatch.config import Schedule
from litwatch.core import Status
from litwatch.core.digests import DigestPeriod, DigestState, week_of
from litwatch.jobs import scheduler
from litwatch.store import digests as store_
from litwatch.store import jobs as jobs_store
from litwatch.store import reading
from litwatch.store import taxonomy as taxonomy_store

router = protected("/api/digests")

NO_DIGEST = {404: {"description": "No such digest, or its topic is no longer shown"}}
WRITE_AGAIN = NO_DIGEST | {
    409: {"description": "The digest is not failed, its topic is not active, or no worker runs"}
}


def _paper(row: dict[str, Any]) -> s.DigestPaper:
    return s.DigestPaper(id=row["paper_id"], revision=row["paper_revision"], **row)


async def _shown(c: psycopg.AsyncConnection) -> list[str]:
    """The shown topics, whose digests the tab shows and steps through."""
    return list(_looks(await taxonomy_store.load(c)))


async def marks(
    c: psycopg.AsyncConnection,
    period: DigestPeriod,
    first: date,
    last: date,
    topic: str | None = None,
) -> list[tuple[dict[str, Any], s.Badge]]:
    """The digests of `period` from `first` to `last`, of shown topics only (of `topic` alone
    when given), newest day first and each day's in the fixed topic order, with their topic's
    badge."""
    looks = _looks(await taxonomy_store.load(c))
    order = {slug: i for i, slug in enumerate(looks)}
    found = await store_.digests_on(c, period, first, last, topic)
    rows = [r for r in found if r["topic"] in looks]
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
    found = await marks(c, period, day, day)
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


# How long past its time the nightly may take to start before today's run counts as late.
GRACE = timedelta(minutes=15)


def today_run(
    day: date, now: datetime, schedule: Schedule, run: dict[str, Any] | None
) -> s.TodayRun | None:
    """Where today's nightly run stands (DESIGN.md, Day), when `day` is today in the schedule's
    zone, the schedule is on and the nightly fires today; else None. `run` is the latest nightly
    run's status (`store.reading.status`)."""
    zone = schedule.zone
    if not schedule.enabled or now.astimezone(zone).date() != day:
        return None
    due = scheduler.last_fire(schedule.nightly, datetime.combine(day, time.max, zone))
    if due.date() != day:
        return None
    deadline = datetime.combine(day, schedule.deadline, zone)
    if deadline <= due:
        deadline += timedelta(days=1)
    night = {"due": due, "deadline": deadline}
    # Today's run started at or after tonight's fire, as the scheduler counts it (`is_due`): a run
    # by hand before it leaves the fire outstanding; one still going shows as it goes.
    started = run.get("started_at") if run else None
    ours = started is not None and started.astimezone(zone).date() == day
    if run is None or not ours or (started < due and run["state"] != "running"):
        return s.TodayRun(state="late" if now >= due + GRACE else "waiting", **night)
    if run["state"] == "running":
        state = "overrun" if now > deadline else "running"
        total = run["papers"] + (run.get("waiting") or 0)
        return s.TodayRun(state=state, papers=run["papers"], total=total, **night)
    if run["state"] == "failed":
        return s.TodayRun(state="failed", error=run.get("error"), **night)
    return s.TodayRun(state="done", **night)


@router.get("/days/{day}")
async def day(day: date, request: Request, c: Conn) -> s.Day:
    """A day's daily digests, every topic's at Gist level, and where ‹ › step; today, where
    its nightly run stands."""
    weekly = None
    shown = await _shown(c)
    schedule = request.app.state.config.schedule
    now = datetime.now(UTC)
    today = schedule.enabled and now.astimezone(schedule.zone).date() == day
    monday = day - timedelta(days=7)
    if day.weekday() == 0 and await marks(c, DigestPeriod.WEEK, monday, monday):
        weekly = monday
    # The run before the cards: it is closed only once its digests are written, so a run read as
    # done never comes with the cards from before its last digest (the reads are not one snapshot).
    run = today_run(day, now, schedule, await reading.status(c)) if today else None
    return s.Day(
        day=day,
        released=await store_.released(c, day),
        digests=await _cards(c, DigestPeriod.DAY, day),
        previous=await store_.step(c, DigestPeriod.DAY, day, False, shown),
        next=await store_.step(c, DigestPeriod.DAY, day, True, shown),
        weekly=weekly,
        run=run,
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
    for r, badge in await marks(c, DigestPeriod.DAY, first, last):
        days.setdefault(r["day"], []).append(s.DigestMark(**_mark(r, badge)))
    weeks: dict[date, list[s.DigestMark]] = {}
    for r, badge in await marks(c, DigestPeriod.WEEK, week_of(first), last):
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
    found = await in_full(c, digest_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such digest")
    return found


async def in_full(c: psycopg.AsyncConnection, digest_id: int) -> s.Digest | None:
    """A digest in full, as /api and /mcp both show it; None when there is none or its topic is
    no longer shown."""
    row = await store_.get(c, digest_id)
    looks = _looks(await taxonomy_store.load(c))
    if row is None or row["topic"] not in looks:
        return None
    return s.Digest(
        **_mark(row, looks[row["topic"]]),
        gist=row["gist"],
        text=row["text"],
        error=row["error"],
        written_at=row["written_at"],
        papers=[_paper(p) for p in await store_.papers_of(c, digest_id)],
    )


async def _with_job(c: psycopg.AsyncConnection, digest_id: int) -> s.Digest:
    """The digest in full with its newest job writing it again, read after it: a job read done
    comes with the digest it wrote."""
    job = await jobs_store.digest_job(c, digest_id)
    found = await _digest(c, digest_id)
    if job is not None:
        found.job = s.JobRef(id=job["id"], state=job["state"], error=job["error"])
    return found


@router.get("/{digest_id}", responses=NO_DIGEST)
async def digest(digest_id: int, c: Conn) -> s.Digest:
    """A digest in full: its text and its papers by role, each as it was shown, and its newest
    job writing it again."""
    return await _with_job(c, digest_id)


@router.post("/{digest_id}/write", status_code=202, responses=WRITE_AGAIN)
async def write_again(digest_id: int, request: Request, c: Conn) -> s.Digest:
    """Write a failed digest again, as a job (DESIGN.md, Jobs): queued now, and written by the
    jobs worker as soon as the run lock is free (a nightly run or another job's chunk may hold
    it), over the papers due for it then; it may fail again. Asked again while its job is
    queued or running, it answers with that job; a failed one goes back in the queue. 409 when
    it is not failed (written meanwhile) or its topic is not active, checked under the digest's
    row lock, or when the schedule is off (no worker runs jobs). Answers with the digest and its
    job."""
    await _digest(c, digest_id)
    if not request.app.state.config.schedule.enabled:
        raise HTTPException(status_code=409, detail="the schedule is off: no worker runs jobs")
    async with c.transaction():
        row = await store_.get(c, digest_id, lock=True)
        tree = await taxonomy_store.load(c)
        topic = tree.topic(row["topic"]) if tree and row else None
        if row is None or row["state"] != DigestState.FAILED or topic is None:
            raise HTTPException(status_code=409, detail="the digest is not one to write again")
        if topic.status != Status.ACTIVE:
            raise HTTPException(status_code=409, detail="the digest is not one to write again")
        await jobs_store.write_digest(c, digest_id)
    request.app.state.jobs_wake.set()
    return await _with_job(c, digest_id)
