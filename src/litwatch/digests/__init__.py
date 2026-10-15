"""Digests (docs/specs/v1/digests.md): each followed topic's written overview of a day or a week,
by its depth. A Focus topic's daily digest lists its due papers at Fit central and related; an
Area's names one or two picks among the papers graded must read; every topic gets a weekly one,
written on Monday for the week before.

A digest covers the papers due when it is written (`store.digests.due`), so a paper that became
eligible late is in the next one. Published digests are final; one whose writer call failed is
failed, its papers stay due, and the next run writes it again. A digest with nothing due is
published as nothing new, without a call."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, timedelta

import psycopg

from litwatch.core import FAILED_RUNS, Depth, Rarity, Status, Taxonomy, Topic
from litwatch.core.digests import DAILY, PUBLISHED, DigestPeriod, DigestRole, DigestState
from litwatch.digests import writer
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
) -> Outcome:
    """A published digest: written when it has a text, else nothing new. Its Mark counts the
    papers it covered (`extra`, an earlier paper brought back, is not one)."""
    best = max((Rarity(s.due.rarity) for s in covered), key=lambda r: r.level, default=None)
    return Outcome(
        topic,
        period,
        day,
        DigestState.WRITTEN if text is not None else DigestState.NOTHING_NEW,
        gist=gist,
        text=text,
        papers=[*covered, *extra],
        counts={
            "papers": len(covered),
            "picks": sum(s.role == DigestRole.PICK for s in covered),
            "central": sum(s.due.fit == "central" for s in covered),
            "related": sum(s.due.fit == "related" for s in covered),
            "must_read": sum(s.due.grade == "must_read" for s in covered),
        },
        best_rarity=best.value if best else None,
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


async def _ask(run: ModelRun, request: Request) -> writer.Written:
    return (await run.gateway.ask(writer.ROLE, request)).value


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
    that does not parse, makes it failed."""
    name = tree.get(topic.slug).name
    weekly = period == DigestPeriod.WEEK
    if weekly and topic.depth in DAILY:  # its week's daily digests' papers
        found = await store_.of_week(conn, tree, topic, day)
    else:
        last = day + timedelta(days=6) if weekly else day
        found = await store_.due(conn, tree, topic, period, last, FAILED_RUNS)
    if not found:
        return _outcome(topic.slug, period, day, [])
    try:
        if not weekly and topic.depth == Depth.FOCUS:
            out = await _ask(run, writer.focus_day(topic, name, day, found))
            return _outcome(topic.slug, period, day, _covered(found), gist=out.gist, text=out.text)
        if not weekly:
            out = await _ask(run, writer.area_day(topic, name, day, found))
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
                out = await _ask(run, request)
                back = [
                    Shown(e, DigestRole.MENTIONED) for e in earlier if e.paper_id == out.recalled
                ]
                covered = _covered(found)
                return _outcome(
                    topic.slug, period, day, covered, gist=out.gist, text=out.text, extra=back
                )
            out = await _ask(run, writer.week(topic, name, day, found, dailies))
    except CallError as exc:
        return Outcome(topic.slug, period, day, DigestState.FAILED, error=str(exc))
    covered = _covered(found, out.picks, out.mentioned)
    return _outcome(topic.slug, period, day, covered, gist=out.gist, text=out.text)


async def write(
    conn: psycopg.AsyncConnection,
    run: ModelRun,
    tree: Taxonomy,
    topic: Topic,
    period: DigestPeriod,
    day: date,
) -> DigestState | None:
    """Write and store the digest, unless it is published already (None: a re-run of the same
    day rewrites nothing). Returns the state stored."""
    if await store_.state_of(conn, topic.slug, period, day) in PUBLISHED:
        return None
    o = await compose(conn, run, tree, topic, period, day)
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
    weekly digests of the week before."""
    await retry_failed(conn, run, tree)
    await write_day(conn, run, tree, today)
    if today.weekday() == 0:
        await write_week(conn, run, tree, today - timedelta(days=7))
