"""Reviews and the weekly issue. A Radar topic with a daily cadence is reviewed after each
nightly run, before the daily issue is built; every Radar and Pulse topic is reviewed weekly,
every Watch topic gets a recap, and the weekly issue gathers them (spec §3, decision 23)."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import psycopg

from litwatch.config import Config
from litwatch.core import Cadence, Paper, Status, Topic, TopicKind
from litwatch.jobs.context import ModelRun
from litwatch.llm import CallError
from litwatch.review import reviews
from litwatch.store import digest as digest_store
from litwatch.store import reviews as reviews_store
from litwatch.store import topics as topics_store

log = logging.getLogger(__name__)


async def _review(
    conn: psycopg.AsyncConnection,
    run: ModelRun,
    topic: Topic,
    period: str,
    day: date,
    papers: list[Paper],
) -> str:
    """Review the topic's members for the period and store it. Returns the review's state."""
    if not papers:
        await reviews_store.put(conn, topic.slug, period, day, kind=topic.kind, state="quiet")
        return "quiet"
    try:
        written = await reviews.review(run.gateway, topic, period, papers)
    except CallError as exc:
        log.warning("%s %s review of %s failed: %s", topic.slug, period, day, exc)
        await reviews_store.put(
            conn,
            topic.slug,
            period,
            day,
            kind=topic.kind,
            state="failed",
            error=str(exc),
            members=len(papers),
        )
        run.add("reviews_failed")
        return "failed"
    await reviews_store.put(
        conn,
        topic.slug,
        period,
        day,
        kind=topic.kind,
        state="written",
        topic_revision=topic.revision,
        text=written.text,
        members=len(papers),
        picks=[(p.id, p.why) for p in written.picks],
    )
    run.add("reviews")
    return "written"


async def daily_reviews(
    conn: psycopg.AsyncConnection, config: Config, run: ModelRun, day: date | None = None
) -> int:
    """Review each daily Radar topic over the papers that joined it on the day. A review
    already written for the day is kept; a failed or quiet one is tried again."""
    day = day or config.schedule.today()
    since, until = config.schedule.window(day)
    done = 0
    for topic in await topics_store.all(conn, Status.ACTIVE):
        if topic.kind != TopicKind.RADAR or topic.cadence != Cadence.DAILY:
            continue
        existing = await reviews_store.get(conn, topic.slug, "day", day)
        if existing and existing["state"] == "written":
            continue
        papers = await reviews_store.members(conn, topic.slug, since, until)
        if await _review(conn, run, topic, "day", day, papers) == "written":
            done += 1
    if done and (issue := await digest_store.issue(conn, "daily", day)):
        await digest_store.mark_lede_stale(conn, issue["id"])
    return done


async def weekly(
    conn: psycopg.AsyncConnection, config: Config, run: ModelRun, monday: date | None = None
) -> int:
    """Review the week (Monday to Sunday, the last full one by default) for every topic and
    build the weekly issue. Running again amends it: what is written stays, failures are
    tried again. Returns the weekly issue's id."""
    monday = monday or config.schedule.week_of(config.schedule.today()) - timedelta(days=7)
    since, until = config.schedule.window(monday, 7)
    changed = False
    for topic in await topics_store.all(conn, Status.ACTIVE):
        existing = await reviews_store.get(conn, topic.slug, "week", monday)
        if existing and existing["state"] == "written":
            continue
        if topic.kind == TopicKind.WATCH:
            state = await _recap(conn, run, topic, monday)
        else:
            papers = await reviews_store.members(conn, topic.slug, since, until)
            state = await _review(conn, run, topic, "week", monday, papers)
        changed |= state == "written"
    issue = await digest_store.open_issue(conn, "weekly", monday)
    if changed:  # stale until rewritten, so a failed rewrite is tried on the next run
        await digest_store.mark_lede_stale(conn, issue["id"])
    elif issue["lede"] and not issue["lede_stale"]:
        return issue["id"]
    written = [
        (r["topic_name"], r["text"])
        for r in await reviews_store.for_period(conn, "week", monday)
        if r["state"] == "written" and r["text"]
    ]
    if written:
        try:
            await digest_store.set_lede(
                conn, issue["id"], await reviews.weekly_lede(run.gateway, written)
            )
        except CallError as exc:
            log.warning("weekly issue %s: no lede (%s)", monday, exc)
            run.add("lede_failed")
    return issue["id"]


async def _recap(conn: psycopg.AsyncConnection, run: ModelRun, topic: Topic, monday: date) -> str:
    """The week's recap for a Watch topic: prose over the week's entries. A week with nothing
    announced is quiet; what was set aside and missed is listed either way."""
    entries = await reviews_store.week_entries(conn, topic.slug, monday)
    if not entries:
        await reviews_store.put(conn, topic.slug, "week", monday, kind=topic.kind, state="quiet")
        return "quiet"
    try:
        text = await reviews.recap(
            run.gateway, topic, [reviews.RecapEntry(e["title"], e["take"]) for e in entries]
        )
    except CallError as exc:
        await reviews_store.put(
            conn,
            topic.slug,
            "week",
            monday,
            kind=topic.kind,
            state="failed",
            error=str(exc),
            members=len(entries),
        )
        run.add("recaps_failed")
        return "failed"
    await reviews_store.put(
        conn,
        topic.slug,
        "week",
        monday,
        kind=topic.kind,
        state="written",
        topic_revision=topic.revision,
        text=text,
        members=len(entries),
    )
    run.add("recaps")
    return "written"
