"""The writer's jobs: takes for every pair that is owed one, then the day's issue."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date

import psycopg

from litwatch import events
from litwatch.config import Config
from litwatch.core import Grade, Opinion, Paper, Take, TakeSource, Topic, TopicKind
from litwatch.jobs.context import ModelRun
from litwatch.llm import CallError, Gateway
from litwatch.review import writer
from litwatch.store import digest as digest_store
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts as verdicts_store
from litwatch.store import word as word_store

log = logging.getLogger(__name__)


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
            source = TakeSource.MIGRATED if verdict.migrated else TakeSource.VERDICT
            opinions = [o for o in (verdict.judge, verdict.check) if o]
            binding = (verdict.paper_revision, verdict.topic_revision)
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
        paper_id, slug = item.paper.id, item.topic.slug
        if item.text is None:
            await digest_store.take_failed(conn, paper_id, slug, item.error or "no text")
            run.add("takes_failed")
            continue
        async with conn.transaction():
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
    ready = await digest_store.due_for_issue(conn, config.limits.take_attempts)
    folds = await digest_store.due_folds(conn)
    existing = await digest_store.issue(conn, "daily", day)
    if not ready and existing is None:
        return None  # adjacent arrivals alone make no issue; they wait for one
    async with conn.transaction():
        issue = await digest_store.open_issue(conn, "daily", day)
        added = 0
        for row in ready:
            grade = (await word_store.effective_for(conn, row["paper_id"])).get(row["topic"])
            if grade is None or not grade.at_least(Grade.RELEVANT):
                await digest_store.clear_due(conn, row["paper_id"], row["topic"])
                continue
            take = await digest_store.latest_take(conn, row["paper_id"], row["topic"])
            added += await digest_store.add_entry(
                conn, issue["id"], row["paper_id"], row["topic"], grade, take.id if take else None
            )
            await digest_store.settle_due(conn, row["paper_id"], row["topic"], issue=False)
        counts: dict[str, int] = {}
        for row in folds:
            counts[row["topic"]] = counts.get(row["topic"], 0) + 1
            await digest_store.settle_due(conn, row["paper_id"], row["topic"], fold=False)
        if counts:
            await digest_store.add_folds(conn, issue["id"], counts)
    run.add("issue_entries", added)
    if added or issue["lede"] is None:
        await _write_lede(conn, run, issue["id"])
    return issue["id"]


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
