"""Looking a paper up by its arXiv link, and reporting it as a miss for a Watch topic.

Filing a miss is the owner's word (a label of relevant, origin `miss`), so it saves the paper
like any label. The trace of what each stage would have said is a dry run, written to the
evaluation tables only.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

import psycopg

from litwatch.config import Config
from litwatch.core import Label, LabelOrigin, LabelValue, Opinion, Paper, TakeSource, Topic
from litwatch.feedback import word
from litwatch.feedback.replay import replay
from litwatch.feedback.word import Settled
from litwatch.intake.search import fetch_by_ids, id_from_url
from litwatch.jobs.context import GatewayFactory, default_gateway
from litwatch.store import digest as digest_store
from litwatch.store import papers as papers_store


class NotFoundError(LookupError):
    """The text names no arXiv paper, or arXiv does not know it."""


async def lookup(conn: psycopg.AsyncConnection, text: str) -> tuple[Paper, bool]:
    """The paper an arXiv id or link names, fetched and stored when the collection lacks it.
    Returns the paper and whether it was fetched just now."""
    try:
        paper_id = id_from_url(text)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    if stored := await papers_store.get(conn, paper_id):
        return stored, False
    fetched = await asyncio.to_thread(fetch_by_ids, [paper_id])
    if not fetched:
        raise NotFoundError(f"arXiv has no paper {paper_id}")
    await papers_store.upsert(conn, fetched)
    return await papers_store.get(conn, paper_id), True


async def report(
    conn: psycopg.AsyncConnection, paper_id: str, topic: str
) -> tuple[int, Settled, bool, bool]:
    """File the miss and give the owner's word on it. Returns the miss id, what settled,
    whether it is new, and whether it needs a trace. Reporting again changes nothing (a
    correction made since stands), except that a trace never finished is asked for again:
    one cut off by a restart would otherwise stay pending. The trace is `trace`'s job."""
    async with conn.transaction():
        row = await (
            await conn.execute(
                "INSERT INTO misses (paper_id, topic) VALUES (%s, %s) "
                "ON CONFLICT (paper_id, topic) DO NOTHING RETURNING id",
                (paper_id, topic),
            )
        ).fetchone()
        if row is None:
            existing = await (
                await conn.execute(
                    "SELECT id, trace FROM misses WHERE paper_id = %s AND topic = %s",
                    (paper_id, topic),
                )
            ).fetchone()
            settled = await word.settle(conn, paper_id)
            return existing["id"], settled, False, await _traceable(conn, existing["id"])
        _, settled = await word.label(
            conn,
            Label(
                paper_id=paper_id, topic=topic, value=LabelValue.RELEVANT, origin=LabelOrigin.MISS
            ),
        )
    return row["id"], settled, True, True


async def trace(
    conn: psycopg.AsyncConnection,
    config: Config,
    miss_id: int,
    gateway: GatewayFactory = default_gateway,
) -> int | None:
    """Run the missed paper through triage, the keyword audit and both graders for every
    Watch topic, as a dry run, and attach the evaluation run to the miss. A failure is
    recorded on the miss, which Settings can trace again; the owner's word is untouched."""
    # Claim the trace: a second request while one runs (a double click, a retry) does nothing.
    row = await (
        await conn.execute(
            f"UPDATE misses SET trace = 'pending', trace_started = now() "
            f"WHERE id = %s AND {_CLAIMABLE} RETURNING paper_id",
            (miss_id, TRACE_PATIENCE),
        )
    ).fetchone()
    if row is None:
        return None
    try:
        paper = await papers_store.get(conn, row["paper_id"])
        eval_run_id = await replay(
            conn, config, [paper], kind="miss", params={"miss": miss_id}, gateway=gateway
        )
    except BaseException as exc:  # a cancelled trace is recorded too, then the cancel goes on
        with contextlib.suppress(Exception):  # the original failure matters more
            await conn.execute(
                "UPDATE misses SET trace = 'failed', trace_error = %s WHERE id = %s",
                (f"{type(exc).__name__}: {exc}"[:500], miss_id),
            )
        if not isinstance(exc, Exception):
            raise
        return None
    async with conn.transaction():
        miss = await (
            await conn.execute(
                "UPDATE misses SET eval_run_id = %s, trace = 'done', trace_error = NULL "
                "WHERE id = %s RETURNING paper_id, topic",
                (eval_run_id, miss_id),
            )
        ).fetchone()
        # A take written before the trace came back had no graders' notes: owe a new one.
        await word.lock(conn, miss["paper_id"])
        take = await digest_store.latest_take(conn, miss["paper_id"], miss["topic"])
        if take is not None and take.source == TakeSource.OWNER:
            await digest_store.mark_due(conn, miss["paper_id"], miss["topic"], take=True)
    return eval_run_id


# A trace can start when none has, when the last one failed or finished, or when one left
# pending has outlived TRACE_PATIENCE.
_CLAIMABLE = "(trace_started IS NULL OR trace <> 'pending' OR trace_started < now() - %s)"


async def _traceable(conn: psycopg.AsyncConnection, miss_id: int) -> bool:
    """Whether reporting the miss again should start a trace: never once one finished, nor
    while one is running."""
    row = await (
        await conn.execute(
            f"SELECT trace <> 'done' AND {_CLAIMABLE} AS ok FROM misses WHERE id = %s",
            (TRACE_PATIENCE, miss_id),
        )
    ).fetchone()
    return bool(row and row["ok"])


class Pending:
    """A reported miss whose trace has not finished: its take waits for the graders' notes."""


# A trace pending longer than this was cut off (a restart, a crash): the take stops waiting
# for it, and is rewritten with the notes if the trace is run again and finishes.
TRACE_PATIENCE = timedelta(hours=1)


async def opinions(
    conn: psycopg.AsyncConnection, paper: Paper, topic: Topic
) -> list[Opinion] | type[Pending]:
    """The graders' opinions from the miss trace of the paper for the topic, for the writer
    when no verdict speaks. Only a trace of this text (the paper's revision) under this rubric
    (topic revision) counts. `Pending` while a reported miss's trace is still running."""
    miss = await (
        await conn.execute(
            "SELECT trace = 'pending' AND coalesce(trace_started, reported_at) > now() - %s "
            "AS waiting FROM misses WHERE paper_id = %s AND topic = %s",
            (TRACE_PATIENCE, paper.id, topic.slug),
        )
    ).fetchone()
    if miss and miss["waiting"]:
        return Pending
    row = await (
        await conn.execute(
            """
            SELECT r.judge, r.check_opinion FROM eval_results r
            JOIN eval_runs e ON e.id = r.eval_run_id
            WHERE e.kind = 'miss' AND r.paper_id = %s AND r.topic = %s
              AND (e.topics->>%s)::int = %s AND r.paper_revision = %s
            ORDER BY e.id DESC LIMIT 1
            """,
            (paper.id, topic.slug, topic.slug, topic.revision, paper.revision),
        )
    ).fetchone()
    if row is None:
        return []
    return [Opinion.model_validate(o) for o in (row["judge"], row["check_opinion"]) if o]


async def reported(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    """Every reported miss with its paper and what each stage said (Settings, Misses)."""
    return await (
        await conn.execute(
            """
            SELECT m.id, m.paper_id, m.topic, m.reported_at, m.eval_run_id, m.trace,
                   m.trace_error, p.title,
                   r.triage_yes, r.triage_why, r.keyword, r.judge, r.check_opinion AS check,
                   r.grade, r.error, r.grade_error
            FROM misses m JOIN papers p ON p.id = m.paper_id
            LEFT JOIN eval_results r ON r.eval_run_id = m.eval_run_id
                 AND r.paper_id = m.paper_id AND r.topic = m.topic
            ORDER BY m.reported_at DESC
            """
        )
    ).fetchall()
