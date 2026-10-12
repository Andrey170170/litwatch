"""The writer's jobs: takes for every pair that is owed one, then the day's issue."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg

from litwatch import events
from litwatch.config import Config
from litwatch.core import Grade, Opinion, Paper, Take, TakeSource, Topic, TopicKind
from litwatch.feedback import word
from litwatch.jobs.context import ModelRun
from litwatch.llm import CallError, Gateway
from litwatch.review import writer
from litwatch.store import digest as digest_store
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts as verdicts_store
from litwatch.store import word as word_store

log = logging.getLogger(__name__)

# A lede that failed is tried again on later builds for this many days.
LEDE_RETRY_DAYS = 7


@dataclass
class Owed:
    """Everything the writer needs for one take, read before any call is made."""

    paper: Paper
    topic: Topic
    grade: Grade
    source: TakeSource
    opinions: list[Opinion]
    binding: tuple[int, int]  # paper revision, topic revision
    text: str | None = None
    error: str | None = None


async def _owed(conn: psycopg.AsyncConnection) -> list[Owed]:
    watched = {t.slug: t for t in await topics_store.all(conn, None) if t.kind == TopicKind.WATCH}
    owed = []
    for row in await digest_store.due_for_take(conn):
        topic = watched.get(row["topic"])
        grade = (await word_store.effective_for(conn, row["paper_id"])).get(row["topic"])
        paper = await papers_store.get(conn, row["paper_id"])
        if topic is None or paper is None or grade is None or not grade.at_least(Grade.RELEVANT):
            await digest_store.clear_due(conn, row["paper_id"], row["topic"])
            continue
        verdict = await verdicts_store.current(conn, paper.id, topic.slug)
        if verdict is None:
            source, opinions = TakeSource.OWNER, []
            binding = (paper.revision, topic.revision or 1)
        else:
            if verdict.paper_revision != paper.revision:
                # Only the current text is stored; the take waits for the rewrite's re-grade,
                # and after enough runs the pair enters its issue without one.
                await digest_store.take_failed(
                    conn, paper.id, topic.slug, "the text was rewritten; waiting for its re-grade"
                )
                continue
            source = TakeSource.MIGRATED if verdict.migrated else TakeSource.VERDICT
            opinions = [o for o in (verdict.judge, verdict.check) if o]
            binding = (verdict.paper_revision, verdict.topic_revision)
            # The rubric the verdict was given under, which the take is bound to.
            topic = await topics_store.at_revision(conn, topic.slug, verdict.topic_revision)
        owed.append(Owed(paper, topic, grade, source, opinions, binding))
    return owed


async def _write_many(owed: list[Owed], gateway: Gateway, concurrency: int) -> AsyncIterator[Owed]:
    limit = asyncio.Semaphore(concurrency)

    async def one(item: Owed) -> Owed:
        async with limit:
            try:
                item.text = await writer.take(
                    gateway, item.paper, item.topic, item.grade, item.opinions
                )
            except CallError as exc:
                item.error = str(exc)
            return item

    tasks = [asyncio.create_task(one(item)) for item in owed]
    try:
        for done in asyncio.as_completed(tasks):
            yield await done
    finally:
        for task in tasks:
            task.cancel()


async def write_takes(conn: psycopg.AsyncConnection, config: Config, run: ModelRun) -> None:
    """Write a take for every pair owed one. A failure leaves the pair owed, counted."""
    owed = await _owed(conn)
    if not owed:
        return
    log.info("run %s writer: %s takes", run.run_id, len(owed))
    async for item in _write_many(owed, run.gateway, config.limits.writing):
        await _record(conn, item, run)


async def _record(conn: psycopg.AsyncConnection, item: Owed, run: ModelRun) -> None:
    """Store a written take, or count the failure. The take is kept whatever happened
    meanwhile; the pair stops being owed one only if it still fits what the pair needs."""
    paper_id, slug = item.paper.id, item.topic.slug
    if item.text is None:
        await digest_store.take_failed(conn, paper_id, slug, item.error or "no text")
        run.add("takes_failed")
        return
    async with conn.transaction():
        # Under the paper's lock: the pair may have moved while the model was writing.
        await word.lock(conn, paper_id)
        take = await digest_store.add_take(
            conn,
            Take(
                paper_id=paper_id,
                topic=slug,
                paper_revision=item.binding[0],
                topic_revision=item.binding[1],
                grade=item.grade,
                source=item.source,
                text=item.text,
            ),
        )
        now = await word.wanted(conn, paper_id, item.topic)
        if now is None or word.fits(take, *now):
            await digest_store.settle_due(conn, paper_id, slug, take=False)
        await events.emit(conn, events.TAKE_WRITTEN, paper_id, topic=slug, take_id=take.id)
    run.add("takes")


async def build_daily(
    conn: psycopg.AsyncConnection, config: Config, run: ModelRun, day: date | None = None
) -> int | None:
    """Take every pair ready for an issue into the day's issue, count the adjacent arrivals,
    and write the lede. Running again the same day amends the issue and never repeats an
    entry. Returns the issue id, or None when the day has no issue."""
    day = day or config.schedule.today()
    attempts = config.limits.take_attempts
    ready = await digest_store.due_for_issue(conn, attempts)
    folds = await digest_store.due_folds(conn)
    existing = await digest_store.issue(conn, "daily", day)
    watched = {t.slug: t for t in await topics_store.all(conn, None) if t.kind == TopicKind.WATCH}
    added = 0
    async with conn.transaction():
        issue = existing  # opened with its first entry: adjacent arrivals alone make none
        for row in ready:
            paper_id, slug = row["paper_id"], row["topic"]
            # Read again under the paper's lock: the owner may have just rejected it.
            await word.lock(conn, paper_id)
            due = await digest_store.due_row(conn, paper_id, slug)
            now = await word.wanted(conn, paper_id, watched[slug]) if slug in watched else None
            if now is None:
                await digest_store.clear_due(conn, paper_id, slug)
                continue
            if not (due and due["for_issue"]):
                continue
            if due["for_take"] and due["failed_runs"] < attempts:
                continue  # a take became due since; it waits for the next issue
            take = await digest_store.latest_take(conn, paper_id, slug)
            take_id = take.id if word.fits(take, *now) else None
            issue = issue or await digest_store.open_issue(conn, "daily", day)
            added += await digest_store.add_entry(
                conn, issue["id"], paper_id, slug, now[0], take_id
            )
            await digest_store.settle_due(conn, paper_id, slug, issue=False)
        counts: dict[str, int] = {}
        for row in folds if issue else []:
            counts[row["topic"]] = counts.get(row["topic"], 0) + 1
            await digest_store.settle_due(conn, row["paper_id"], row["topic"], fold=False)
        if counts:
            await digest_store.add_folds(conn, issue["id"], counts)
    run.add("issue_entries", added)
    # Ledes missing or older than their entries, this issue's and any recent one's.
    for issue_id in await digest_store.lede_owed(conn, day - timedelta(days=LEDE_RETRY_DAYS)):
        await _write_lede(conn, run, issue_id)
    found = await digest_store.issue(conn, "daily", day)
    return found["id"] if found else None


async def _write_lede(conn: psycopg.AsyncConnection, run: ModelRun, issue_id: int) -> None:
    """Write the lede from the issue as it now stands. A failure leaves the old one, or none,
    and the next build tries again."""
    rows = await digest_store.entries(conn, issue_id)
    if not rows:
        return
    entries = [
        writer.Entry(r["topic_name"], r["title"], r["take"], r["grade"] == "must_read")
        for r in rows
    ]
    issue = await (
        await conn.execute("SELECT folds FROM issues WHERE id = %s", (issue_id,))
    ).fetchone()
    names = {t.slug: t.name for t in await topics_store.all(conn, None)}
    folds = {names.get(k, k): v for k, v in issue["folds"].items()}
    try:
        await digest_store.set_lede(conn, issue_id, await writer.lede(run.gateway, entries, folds))
    except CallError as exc:
        log.warning("issue %s: no lede (%s)", issue_id, exc)
        run.add("lede_failed")
