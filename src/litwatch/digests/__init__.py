"""Digests (docs/specs/v1/digests.md): each followed topic's written overview of a day or a week,
by its depth. A Focus topic's daily digest lists its due papers at Fit central and related; an
Area's names one or two picks among the papers graded must read; every topic gets a weekly one,
written on Monday for the week before.

A digest covers the papers due when it is written (`store.digests.due`), so a paper that became
eligible late is in the next one. Published digests are final; one whose writer call failed is
failed, its papers stay due, and the next run writes it again, one model further down `writer`'s
list after each failed answer in a row, until a digest of the topic is published (models.md,
roles). A digest with nothing due is published as nothing new, without a call. Its picks are
settled as it is published, so an Area pick gets its Deep card (`feedback.word.settle`).

An old paper back with a new arXiv version is listed once more as updated
(`store.digests.due_updated`), after the digest's papers and apart from them: the writer is not
given it, and it is in none of the digest's counts of new papers, best rarity or picks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import date, timedelta

import psycopg

from litwatch import store
from litwatch.core import FAILED_RUNS, Cause, Depth, Rarity, Status, Taxonomy, Topic
from litwatch.core.digests import DAILY, PUBLISHED, DigestPeriod, DigestRole, DigestState
from litwatch.digests import writer
from litwatch.feedback import word
from litwatch.jobs.context import ModelRun
from litwatch.llm import CallError, Request
from litwatch.store import digests as store_
from litwatch.store.digests import Due, Outcome, Shown

log = logging.getLogger(__name__)


def _outcome(
    topic: str,
    period: DigestPeriod,
    day: date,
    covered: list[Shown],
    *,
    gist: str | None = None,
    text: str | None = None,
    extra: Sequence[Shown] = (),
    updated: Sequence[Shown] = (),
    model: str | None = None,
) -> Outcome:
    """A published digest: written when it has a text, else nothing new. Its Mark counts the
    papers it covered (`extra`, an earlier paper brought back, is not one), and its `updated`
    papers apart: they are in neither its counts of new papers nor its best rarity."""
    best = max((Rarity(s.due.rarity) for s in covered), key=lambda r: r.level, default=None)
    return Outcome(
        topic,
        period,
        day,
        DigestState.WRITTEN if text is not None else DigestState.NOTHING_NEW,
        gist=gist,
        text=text,
        papers=[*covered, *extra, *updated],
        counts={
            "papers": len(covered),
            "picks": sum(s.role == DigestRole.PICK for s in covered),
            "central": sum(s.due.fit == "central" for s in covered),
            "related": sum(s.due.fit == "related" for s in covered),
            "must_read": sum(s.due.grade == "must_read" for s in covered),
            "updated": len(updated),
        },
        best_rarity=best.value if best else None,
        model=model,
    )


def _covered(
    found: Sequence[Due], picks: Sequence[writer.Pick] = (), mentioned: Sequence[str] = ()
) -> list[Shown]:
    """Every paper the digest covers, in its order: its picks as the writer gave them, the other
    papers its text names, then the rest, central before related and rarest first."""
    by_id = {d.paper_id: d for d in found}
    first = [Shown(by_id[p.paper], DigestRole.PICK, p.why) for p in picks]
    first += [Shown(by_id[m], DigestRole.MENTIONED) for m in mentioned]
    taken = {s.due.paper_id for s in first}
    rest = [Shown(d, DigestRole.LISTED) for d in found if d.paper_id not in taken]
    return first + sorted(rest, key=lambda s: s.due.fit != "central")


class _Writer:
    """The topic's writer calls, from one model further down `writer`'s list than its latest
    failed digest's (the last model is kept), and which model answered."""

    def __init__(self, run: ModelRun, failed: str | None) -> None:
        self.labels = [m.label for m in run.gateway.models.role(writer.ROLE).models]
        self.run, self.failed = run, failed
        self.start = self.labels.index(failed) + 1 if failed in self.labels else 0
        self.model: str | None = None

    async def ask(self, request: Request) -> writer.Written:
        answer = await self.run.gateway.ask(writer.ROLE, request, start=self.start)
        self.model = answer.model
        return answer.value

    def failure(self, exc: CallError) -> str | None:
        """The model a failed digest records: the one its attempt started from when a model
        answered unusably or refused (a refusal can go on past the last model to the top, so
        the next attempt still starts further down), or the one before when no model answered
        (a call with no answer moves nothing down)."""
        if not exc.unusable:
            return self.failed
        return self.labels[min(self.start, len(self.labels) - 1)]


async def compose(
    conn: psycopg.AsyncConnection,
    run: ModelRun,
    tree: Taxonomy,
    topic: Topic,
    period: DigestPeriod,
    day: date,
) -> Outcome:
    """The topic's digest of `period` on `day` (a week's Monday) over the papers due now, not
    stored. With nothing due it is nothing new, without a call; a failed call, or an answer
    that does not parse, makes it failed. Its updated papers are listed after the rest, and
    the writer is not given them: alone, they make a nothing new digest."""
    name = tree.get(topic.slug).name
    weekly = period == DigestPeriod.WEEK
    last = day + timedelta(days=6) if weekly else day
    recap = weekly and topic.depth in DAILY
    if recap:  # its week's daily digests' papers
        found = await store_.of_week(conn, tree, topic, day)
    else:
        found = await store_.due(conn, tree, topic, period, last, FAILED_RUNS)
    back = await store_.due_updated(
        conn, tree, topic, period, last, FAILED_RUNS, week=day if recap else None
    )
    updated = [Shown(d, DigestRole.UPDATED) for d in back]
    if not found:
        return _outcome(topic.slug, period, day, [], updated=updated)
    w = _Writer(run, await store_.failed_writer(conn, topic.slug))
    try:
        if not weekly and topic.depth == Depth.FOCUS:
            out = await w.ask(writer.focus_day(topic, name, day, found))
            return _outcome(
                topic.slug,
                period,
                day,
                _covered(found),
                gist=out.gist,
                text=out.text,
                updated=updated,
                model=w.model,
            )
        if not weekly:
            out = await w.ask(writer.area_day(topic, name, day, found))
        else:
            dailies = await store_.daily_texts(conn, topic.slug, day)
            if topic.depth == Depth.FOCUS:
                ids = {d.paper_id for d in found}
                earlier = [
                    e
                    for e in await store_.earlier_central(conn, topic.slug, day)
                    if e.paper_id not in ids
                ]
                request = writer.focus_week(topic, name, day, found, dailies, earlier)
                out = await w.ask(request)
                recalled = [
                    Shown(e, DigestRole.MENTIONED) for e in earlier if e.paper_id == out.recalled
                ]
                covered = _covered(found)
                return _outcome(
                    topic.slug,
                    period,
                    day,
                    covered,
                    gist=out.gist,
                    text=out.text,
                    extra=recalled,
                    updated=updated,
                    model=w.model,
                )
            out = await w.ask(writer.week(topic, name, day, found, dailies))
    except CallError as exc:
        failed = w.failure(exc)
        return Outcome(topic.slug, period, day, DigestState.FAILED, error=str(exc), model=failed)
    covered = _covered(found, out.picks, out.mentioned)
    return _outcome(
        topic.slug,
        period,
        day,
        covered,
        gist=out.gist,
        text=out.text,
        updated=updated,
        model=w.model,
    )


async def write(
    conn: psycopg.AsyncConnection,
    run: ModelRun,
    tree: Taxonomy,
    topic: Topic,
    period: DigestPeriod,
    day: date,
) -> DigestState | None:
    """Write and store the digest, unless it is published already (None: a re-run of the same
    day rewrites nothing), as one change of its picks (`word.change`): an Area pick's Deep pass
    is queued with it. Returns the state stored."""
    if await store_.state_of(conn, topic.slug, period, day) in PUBLISHED:
        return None
    o = await compose(conn, run, tree, topic, period, day)
    picks = {s.due.paper_id for s in o.papers if s.role == DigestRole.PICK}
    async with word.change(conn, *picks, cause=Cause.VERDICT):  # locked together, in id order
        if await store_.record(conn, o, run.run_id) is None:
            return None
    if o.error:
        log.warning("%s digest of %s for %s failed: %s", period, day, topic.slug, o.error)
    run.add(f"digests_{o.state.value}")
    return o.state


def _active(tree: Taxonomy, depths: Sequence[Depth] = tuple(Depth)) -> list[Topic]:
    """The active topics at `depths`, in the fixed order: a draft or paused topic gets none."""
    return [t for t in tree.shown() if t.status == Status.ACTIVE and t.depth in depths]


async def write_day(
    conn: psycopg.AsyncConnection, run: ModelRun, tree: Taxonomy, day: date
) -> bool:
    """The daily digests of `day`, every active Focus and Area topic's in the fixed order, when
    arXiv listed papers on it; a day it listed none has no digests. Returns whether it did."""
    if not await store_.released(conn, day):
        return False
    for topic in _active(tree, DAILY):
        await write(conn, run, tree, topic, DigestPeriod.DAY, day)
    return True


async def write_week(
    conn: psycopg.AsyncConnection, run: ModelRun, tree: Taxonomy, monday: date
) -> None:
    """The weekly digests of the week from `monday`, every active topic's in the fixed order."""
    for topic in _active(tree):
        await write(conn, run, tree, topic, DigestPeriod.WEEK, monday)


async def retry_failed(conn: psycopg.AsyncConnection, run: ModelRun, tree: Taxonomy) -> None:
    """Write every failed digest of an active topic again, oldest first, each over the papers
    due for it now, so a later digest does not take its papers first."""
    active = {t.slug: t for t in _active(tree)}
    for row in await store_.failed(conn):
        await write(conn, run, tree, active[row["topic"]], DigestPeriod(row["period"]), row["day"])


async def nightly(
    conn: psycopg.AsyncConnection, run: ModelRun, tree: Taxonomy, today: date
) -> None:
    """The night's digests: the failed ones again, today's daily digests, and on a Monday the
    weekly digests of the week before. Each topic's are written in that order, the topics
    concurrently, each on a connection of its own, so a batching run's writer calls go out
    together (digests.md, writing)."""
    failed = await store_.failed(conn)
    released = await store_.released(conn, today)

    async def topic_night(topic: Topic) -> None:
        async with await store.sibling(conn) as c:
            for row in failed:
                if row["topic"] == topic.slug:
                    await write(c, run, tree, topic, DigestPeriod(row["period"]), row["day"])
            if released and topic.depth in DAILY:
                await write(c, run, tree, topic, DigestPeriod.DAY, today)
            if today.weekday() == 0:
                await write(c, run, tree, topic, DigestPeriod.WEEK, today - timedelta(days=7))

    async with asyncio.TaskGroup() as group:
        for topic in _active(tree):
            group.create_task(topic_night(topic))
