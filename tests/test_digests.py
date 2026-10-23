"""Digests: the arXiv listing a paper belongs to, which papers are due for each depth's digests,
quiet days, a published digest as a record, the bounded wait before them, failed digests, the
voice check, the papers a text marks, and the Digest tab's API. The `writer` role is scripted."""

import asyncio
import json
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from litwatch import digests, fulltext, store
from litwatch.api import create_app
from litwatch.api.digests import today_run
from litwatch.config import Config, Schedule
from litwatch.core import Label, LabelValue, Status
from litwatch.core.classification import Source
from litwatch.core.digests import DigestPeriod, listing_day, plain, voice_problems
from litwatch.digests import writer
from litwatch.feedback import word
from litwatch.jobs import digests as digests_job
from litwatch.jobs import nightly, worker
from litwatch.jobs.context import model_run
from litwatch.jobs.lock import run_lock
from litwatch.llm import Gateway, ProviderError, Reply, Stop, Usage
from litwatch.store import classifications as classifications_store
from litwatch.store import digests as store_
from litwatch.store import papers as papers_store
from litwatch.store import reading, runs, verdicts
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import AGENTS, FAITH, INTERP, TREE, paper
from tests.fakes import ScriptedProvider
from tests.test_reading import analyse, classify, grade

TUE, WED, THU, SAT = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 10)
MON = date(2026, 10, 5)
TOKEN = "t"
P = paper("2610.00001", title="Traces that hide steps")
Q = paper("2610.00002", title="A near neighbour")
R = paper("2610.00003", title="A distant cousin")
S = paper("2610.00004", title="Late to the party")


class Writer:
    """The `writer` role, scripted: a valid answer built from the request's answer model (the
    first candidates as picks, a Focus daily marking its first paper, a Focus weekly bringing
    back the first earlier paper), or each of `failures` raised in turn first."""

    def __init__(self) -> None:
        self.failures: list[Exception] = []
        self.asked: list[str] = []

    def respond(self, role, request) -> str:
        self.asked.append(request.user)
        if self.failures:
            raise self.failures.pop(0)
        out = request.output
        answer = {"text": " ".join(["Probes recover hidden steps."] * 20), "gist": "words " * 40}
        if issubclass(out, writer.Picked):
            n = out.picks_range[1] if out.picks_range[0] else min(1, out.picks_range[1])
            picks = sorted(out.candidates)[:n]
            answer |= {"picks": [{"paper": p, "why": "It stands out."} for p in picks]}
        if issubclass(out, writer.FocusDay):
            answer["text"] += f" [The first]({min(out.papers)}) shows it."
        if issubclass(out, writer.FocusWeek) and out.earlier:
            answer["text"] += f" As [an earlier paper]({min(out.earlier)}) found."
        return json.dumps(answer)

    def gateway(self, config: Config, journal) -> Gateway:
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=(0.0,))


@pytest.fixture
async def db(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P, Q, R, S])
    return conn


async def listed(conn, day: date, *papers) -> None:
    await conn.execute(
        "UPDATE papers SET announced = %s WHERE id = ANY(%s)", (day, [p.id for p in papers])
    )


async def focus(conn, p, fit: str) -> None:
    """Graded on the Focus topic at `fit`."""
    await classify(conn, p, FAITH.slug)
    await analyse(conn, p)
    await grade(conn, p, fit)


async def area(conn, p, fit: str, weight: str = "solid") -> None:
    """A member of the Area topic's class, graded there at `fit`, grading finished."""
    await classify(conn, p, INTERP.slug)
    await analyse(conn, p, weight)
    await grade(conn, p, fit, INTERP)
    await verdicts.passed(conn, [(p.id, 1)])


async def day(conn, w: Writer, on: date, config: Config | None = None) -> bool:
    tree = await taxonomy_store.load(conn)
    async with model_run(conn, config or Config(), "digests", gateway=w.gateway) as run:
        return await digests.write_day(conn, run, tree, on)


async def week(conn, w: Writer, monday: date) -> None:
    tree = await taxonomy_store.load(conn)
    async with model_run(conn, Config(), "digests", gateway=w.gateway) as run:
        await digests.write_week(conn, run, tree, monday)


async def shown(conn, topic: str, period: str, on: date) -> tuple[str, list[tuple[str, str]]]:
    """The digest's state, and its papers with their roles in its order."""
    row = await (
        await conn.execute(
            "SELECT id, state FROM digests WHERE topic = %s AND period = %s AND day = %s",
            (topic, period, on),
        )
    ).fetchone()
    if row is None:
        return None, []
    return row["state"], [
        (p["paper_id"], p["role"]) for p in await store_.papers_of(conn, row["id"])
    ]


# -- the listing a paper belongs to ----------------------------------------------------------------


def eastern(day: date, hour: int, minute: int = 0) -> datetime:
    from litwatch.core.digests import ARXIV_ZONE  # noqa: PLC0415

    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ARXIV_ZONE)


def test_a_paper_is_listed_the_weekday_after_the_deadline_it_made():
    assert listing_day(eastern(MON, 13, 59)) == TUE
    assert listing_day(eastern(MON, 14, 0)) == WED
    assert listing_day(eastern(date(2026, 10, 9), 15)) == date(2026, 10, 13)  # Fri late: Tue
    assert listing_day(eastern(SAT, 10)) == date(2026, 10, 13)
    assert listing_day(MON) == TUE  # a date alone is taken as before its deadline
    # 18:30 UTC is 14:30 Eastern in October (late), 13:30 in December (in time).
    assert listing_day(datetime(2026, 10, 5, 18, 30, tzinfo=UTC)) == WED
    assert listing_day(datetime(2026, 12, 7, 18, 30, tzinfo=UTC)) == date(2026, 12, 8)


async def test_the_migration_dates_stored_papers_as_the_code_does(conn):
    """0024 backfills `announced` in SQL; it agrees with `core.digests.announced`."""
    late = paper("2610.00011", version_history=[{"version": 1, "date": "2026-10-05T18:30:00Z"}])
    early = paper("2610.00012", version_history=[{"version": 1, "date": "2026-10-05T17:00:00Z"}])
    weekend = paper("2610.00013", version_history=[{"version": 1, "date": "2026-10-10T12:00:00Z"}])
    friday = paper("2610.00014", published="2026-10-09")
    searched = paper("2610.00015", published="2026-10-05T18:30:00+00:00")  # arXiv search's time
    found = [late, early, weekend, friday, searched]
    await papers_store.upsert(conn, found)
    by_code = {
        r["id"]: r["announced"]
        for r in await (await conn.execute("SELECT id, announced FROM papers")).fetchall()
    }
    assert by_code == {
        late.id: WED,
        early.id: TUE,
        weekend.id: date(2026, 10, 13),
        friday.id: date(2026, 10, 12),
        searched.id: WED,
    }
    sql = Path("src/litwatch/store/migrations/0024_digests.sql").read_text()
    backfill = re.search(r"WITH submitted AS .*?;", sql, re.S).group(0)
    await conn.execute("UPDATE papers SET announced = NULL")
    await conn.execute(backfill)
    by_sql = {
        r["id"]: r["announced"]
        for r in await (await conn.execute("SELECT id, announced FROM papers")).fetchall()
    }
    assert by_sql == by_code


# -- due, per depth --------------------------------------------------------------------------------


async def test_a_focus_daily_lists_central_then_related_and_each_paper_once(db):
    w = Writer()
    await focus(db, P, "related")
    await focus(db, Q, "central")
    await focus(db, R, "adjacent")
    await listed(db, TUE, P, Q, R)
    assert await day(db, w, TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == (
        "written",
        [(Q.id, "listed"), (P.id, "listed")],
    )
    # The next day lists only what became due since; P and Q were announced already.
    await focus(db, S, "central")
    await listed(db, WED, S)
    await day(db, w, WED)
    assert await shown(db, FAITH.slug, "day", WED) == ("written", [(S.id, "listed")])


async def test_the_owners_word_never_makes_a_paper_due(db):
    await focus(db, P, "adjacent")
    await listed(db, TUE, P)
    label = Label(paper_id=P.id, topic=FAITH.slug, value=LabelValue.CENTRAL, origin="reader")
    await word.label(db, label)
    await day(db, Writer(), TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == ("nothing_new", [])


async def test_a_late_paper_is_in_the_next_daily_with_its_own_listing_day(db):
    """Graded after Tuesday's digest (its grading failed that night), P is due on Wednesday."""
    w = Writer()
    await listed(db, TUE, P)
    await day(db, w, TUE)
    await focus(db, P, "central")
    await listed(db, WED, S)
    await day(db, w, WED)
    assert await shown(db, FAITH.slug, "day", WED) == ("written", [(P.id, "listed")])
    assert "(listed Tue 6 Oct 2026)" in w.asked[-1]


async def test_an_area_must_read_waits_for_its_weight_recheck(db):
    """Graded must read from its abstract, P waits for the re-check its grading queued; Q,
    relevant, does not. Once the re-check is done P is due, and a pick candidate."""
    w = Writer()
    await area(db, P, "central")  # central and solid: must read
    await area(db, Q, "related")  # related and solid: relevant
    await listed(db, TUE, P, Q)
    checks = await (
        await db.execute("SELECT kind, state FROM fulltext_passes WHERE paper_id = %s", (P.id,))
    ).fetchall()
    assert ("weight", "awaiting_text") in {(c["kind"], c["state"]) for c in checks}
    await day(db, w, TUE)
    assert await shown(db, INTERP.slug, "day", TUE) == ("written", [(Q.id, "listed")])
    await db.execute("UPDATE fulltext_passes SET state = 'done' WHERE paper_id = %s", (P.id,))
    await listed(db, WED, S)
    await day(db, w, WED)
    assert await shown(db, INTERP.slug, "day", WED) == ("written", [(P.id, "pick")])


async def test_a_terminal_recheck_makes_the_paper_due_on_its_abstract_weight(db):
    await area(db, P, "central")
    await listed(db, TUE, P)
    await db.execute(
        "UPDATE fulltext_passes SET state = 'failed', failed_runs = 3 WHERE paper_id = %s",
        (P.id,),
    )
    await day(db, Writer(), TUE)
    assert await shown(db, INTERP.slug, "day", TUE) == ("written", [(P.id, "pick")])


async def test_an_area_paper_waits_while_its_grading_is_unfinished(db):
    await classify(db, P, INTERP.slug)
    await analyse(db, P)
    await listed(db, TUE, P)
    await day(db, Writer(), TUE)  # classified, not graded yet: no grading pass
    assert await shown(db, INTERP.slug, "day", TUE) == ("nothing_new", [])


async def test_a_field_paper_is_due_until_a_weekly_digest_covers_it(db):
    w = Writer()
    for p in (P, Q):
        await classify(db, p, AGENTS.slug)
        await analyse(db, p)
        await grade(db, p, "related", AGENTS)
        await verdicts.passed(db, [(p.id, 1)])
    await listed(db, TUE, P)
    await listed(db, date(2026, 10, 13), Q)
    await week(db, w, MON)
    state, papers = await shown(db, AGENTS.slug, "week", MON)
    assert (state, {p for p, _ in papers}) == ("written", {P.id})
    assert len([p for p, role in papers if role == "pick"]) == 1  # min(3, the week's papers)
    await week(db, w, date(2026, 10, 12))
    assert {p for p, _ in (await shown(db, AGENTS.slug, "week", date(2026, 10, 12)))[1]} == {Q.id}


async def test_a_focus_weekly_covers_its_dailies_and_may_bring_back_an_earlier_paper(db):
    w = Writer()
    await focus(db, P, "central")
    await listed(db, date(2026, 9, 29), P)  # the week before
    await day(db, w, date(2026, 9, 29))
    await week(db, w, date(2026, 9, 28))
    await db.execute("UPDATE papers SET title = 'Renamed since' WHERE id = %s", (P.id,))
    await focus(db, Q, "central")
    await focus(db, R, "related")
    await listed(db, TUE, Q)
    await listed(db, THU, R)
    await day(db, w, TUE)
    await day(db, w, THU)
    await week(db, w, MON)
    state, papers = await shown(db, FAITH.slug, "week", MON)
    assert (state, papers) == ("written", [(Q.id, "listed"), (R.id, "listed"), (P.id, "mentioned")])
    row = await (
        await db.execute(
            "SELECT counts FROM digests WHERE topic = %s AND period = 'week' AND day = %s",
            (FAITH.slug, MON),
        )
    ).fetchone()
    assert row["counts"]["papers"] == 2  # the paper brought back is not the week's
    assert P.title in w.asked[-1] and "Renamed since" not in w.asked[-1]  # as it was shown
    assert "The first shows it" in w.asked[-1] and "](" not in w.asked[-1]  # dailies, unmarked


async def test_the_owners_word_neither_makes_an_area_paper_due_nor_keeps_one(db):
    """A label related on a paper outside the Area's subtree makes it no member; a label
    adjacent on a member takes it out."""
    await classify(db, P, AGENTS.slug)
    await analyse(db, P)
    await verdicts.passed(db, [(P.id, 1)])
    await word.label(
        db, Label(paper_id=P.id, topic=INTERP.slug, value=LabelValue.RELATED, origin="reader")
    )
    await area(db, Q, "related")
    await word.label(
        db, Label(paper_id=Q.id, topic=INTERP.slug, value=LabelValue.ADJACENT, origin="reader")
    )
    await area(db, R, "related")
    await listed(db, TUE, P, Q, R)
    await day(db, Writer(), TUE)
    assert await shown(db, INTERP.slug, "day", TUE) == ("written", [(R.id, "listed")])


async def test_a_paper_the_keyword_check_sent_to_a_focus_topic_is_in_its_areas_daily(db):
    """No member of the Area's class by classification, it reached the Focus topic by the
    keyword check; graded related there, it is the Area's too, and graded adjacent, it is not."""
    for p, fit in ((P, "related"), (Q, "adjacent")):
        await classify(db, p, keyword=(FAITH.slug,))
        await analyse(db, p)
        await grade(db, p, fit)
    await verdicts.passed(db, [(P.id, 1), (Q.id, 1)])
    await listed(db, TUE, P, Q)
    await day(db, Writer(), TUE)
    state, found = await shown(db, INTERP.slug, "day", TUE)
    assert state == "written" and [i for i, _ in found] == [P.id]


async def test_a_reported_miss_graded_central_is_due_neither_for_its_topic_nor_above(db):
    """Grading follows the owner's report of a miss, but the owner already knows the paper."""
    await classify(db, P)
    await analyse(db, P)
    version = (await taxonomy_store.load(db)).get(FAITH.slug).version
    await classifications_store.put_owner(db, P, FAITH.slug, True, version, source=Source.MISS)
    await grade(db, P, "central")
    await verdicts.passed(db, [(P.id, 1)])
    await listed(db, TUE, P)
    await day(db, Writer(), TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == ("nothing_new", [])
    assert await shown(db, INTERP.slug, "day", TUE) == ("nothing_new", [])


async def test_a_rewritten_paper_graded_again_where_it_was_held_is_due_there(db):
    """Graded adjacent, then rewritten and classified elsewhere: grading follows it to the topic
    from its older text (pipeline.md, section 8), and a related verdict makes it due there and
    in the Area above."""
    await focus(db, P, "adjacent")
    await verdicts.passed(db, [(P.id, 1)])
    rewritten = P.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(db, [rewritten])
    await classify(db, rewritten)
    await analyse(db, rewritten)
    await grade(db, rewritten, "related")
    await verdicts.passed(db, [(P.id, 2)])
    await listed(db, TUE, P)
    await day(db, Writer(), TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == ("written", [(P.id, "listed")])
    assert await shown(db, INTERP.slug, "day", TUE) == ("written", [(P.id, "listed")])


async def test_an_owners_class_label_taking_a_held_paper_out_keeps_it_from_being_due(db):
    """Held on the Focus topic from its older text, but the owner has since taken it out of
    that class: grading follows it there, and its new verdict makes it due nowhere."""
    await focus(db, P, "adjacent")
    await verdicts.passed(db, [(P.id, 1)])
    rewritten = P.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(db, [rewritten])
    await classify(db, rewritten)
    await classifications_store.put_owner(
        db, rewritten, FAITH.slug, False, TREE.get(FAITH.slug).version
    )
    await analyse(db, rewritten)
    await grade(db, rewritten, "related")
    await verdicts.passed(db, [(P.id, 2)])
    await listed(db, TUE, P)
    await day(db, Writer(), TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == ("nothing_new", [])
    assert await shown(db, INTERP.slug, "day", TUE) == ("nothing_new", [])


async def test_a_rewritten_focus_paper_waits_for_its_new_texts_grading(db):
    await focus(db, P, "central")
    rewritten = P.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(db, [rewritten])
    await listed(db, TUE, P)
    await listed(db, WED, S)
    await day(db, Writer(), TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == ("nothing_new", [])
    await grade(db, rewritten, "central")
    await day(db, Writer(), WED)
    assert await shown(db, FAITH.slug, "day", WED) == ("written", [(P.id, "listed")])


async def test_a_grading_failed_on_a_paused_topic_does_not_hold_back_its_area(db):
    """A paused topic grades nothing, so its failed grading is not waited for."""
    await area(db, Q, "related")
    await db.execute(
        "INSERT INTO step_failures (paper_id, step, paper_revision, failed_runs, last_error,"
        " topic) VALUES (%s, 'grading', 1, 1, 'down', %s)",
        (Q.id, FAITH.slug),
    )
    await listed(db, TUE, Q)
    await taxonomy_store.save(
        db, TREE.following(FAITH.model_copy(update={"status": Status.PAUSED}))
    )
    await day(db, Writer(), TUE)
    assert await shown(db, INTERP.slug, "day", TUE) == ("written", [(Q.id, "listed")])


async def test_a_focus_paper_without_its_brief_is_written_from_its_abstract(db):
    w = Writer()
    late = paper("2610.00021", title="No Brief yet", abstract="Probes read the hidden steps.")
    await papers_store.upsert(db, [late])
    await focus(db, late, "central")
    await listed(db, TUE, late)
    await day(db, w, TUE)
    assert "Abstract: Probes read the hidden steps." in w.asked[0]


async def test_a_feed_without_submission_times_dates_a_paper_by_its_listing(db):
    """Atom, the fallback when OAI is out, carries the listing day and no submission time."""
    from litwatch.intake.atom import parse_feed  # noqa: PLC0415
    from tests.test_intake_atom import entry, feed  # noqa: PLC0415

    batch = parse_feed(feed(entry("2609.11999v1")))
    await papers_store.upsert(db, batch.papers, source="atom", listed=batch.listed)
    row = await (
        await db.execute("SELECT announced FROM papers WHERE id = '2609.11999'")
    ).fetchone()
    assert row["announced"] == date(2026, 9, 14)


async def test_a_cross_listing_is_not_dated_by_its_feed_and_canonical_metadata_corrects(db):
    """A cross-listing's first listing is not the feed's day, so Atom leaves it undated; a new
    paper's feed date stands until canonical metadata dates its first version."""
    from litwatch.intake.atom import parse_feed  # noqa: PLC0415
    from tests.test_intake_atom import entry, feed  # noqa: PLC0415

    batch = parse_feed(feed(entry("2609.11999v1") + entry("2401.00001v3", "cross")))
    await papers_store.upsert(db, batch.papers, source="atom", listed=batch.listed)
    oai = paper("2609.11999", title="A paper", published="2026-09-10")
    await papers_store.upsert(db, [oai])
    rows = await (
        await db.execute(
            "SELECT id, announced FROM papers WHERE id IN ('2609.11999', '2401.00001')"
        )
    ).fetchall()
    assert {r["id"]: r["announced"] for r in rows} == {
        "2609.11999": date(2026, 9, 11),
        "2401.00001": None,
    }


def history(*dates: str) -> list[dict]:
    """A version history, v1 first, from the versions' submission times."""
    return [{"version": i, "date": d} for i, d in enumerate(dates, 1)]


def oai(id: str, created: str, version_history=(), **more):
    """A record as OAI gives it, dated by day; with no `version_history`, its version list
    failed to load, so its first version's day is guessed from `<created>`."""
    at = f"{created}T00:00:00+00:00"
    found = list(version_history)
    version = found[-1]["version"] if found else None
    return paper(id, published=at, updated=at, version=version, version_history=found, **more)


async def day_of(conn, p) -> date:
    row = await (
        await conn.execute("SELECT announced FROM papers WHERE id = %s", (p.id,))
    ).fetchone()
    return row["announced"]


async def test_a_failed_version_list_never_moves_a_known_first_day(db):
    """Read from v1's submission time, a first day stands against a record whose version list
    failed to load, whose `<created>` is a later version's date; and against a second one,
    after the first cleared the stored history."""
    old = oai("2605.00001", "2026-05-04", history("2026-05-04T15:00:00+00:00", "2026-05-08"))
    await papers_store.upsert(db, [old], source="oai")
    assert await day_of(db, old) == date(2026, 5, 5)
    for created in ("2026-10-12", "2026-10-14"):
        await papers_store.upsert(db, [oai(old.id, created)], source="oai")
        assert await day_of(db, old) == date(2026, 5, 5)


async def test_a_version_history_corrects_a_guessed_first_day_either_way(db):
    """A Monday `<created>` reads as Tuesday's listing, a Monday-afternoon v1 is Wednesday's;
    a `<created>` that was a later version's date reads months late."""
    early = oai("2610.00031", "2026-10-05")
    late = oai("2610.00032", "2026-10-12")
    await papers_store.upsert(db, [early, late], source="oai")
    assert (await day_of(db, early), await day_of(db, late)) == (TUE, date(2026, 10, 13))
    known = [
        oai(early.id, "2026-10-05", history("2026-10-05T19:00:00+00:00")),
        oai(late.id, "2026-10-12", history("2026-05-04T15:00:00+00:00", "2026-10-12")),
    ]
    await papers_store.upsert(db, known, source="oai")
    assert (await day_of(db, early), await day_of(db, late)) == (WED, date(2026, 5, 5))


async def test_a_paper_due_late_in_the_week_is_in_that_weekly_once(db):
    """P's re-check finishes on Saturday, after Friday's digest: Monday's daily covers it,
    and the weekly of P's week, written after it, has it; the next weekly does not, and the
    weekly before does not take Monday's own paper."""
    w = Writer()
    fri, mon2 = date(2026, 10, 9), date(2026, 10, 12)
    await area(db, P, "central")  # must read: waits for its re-check
    await area(db, Q, "related")
    await listed(db, fri, P, Q)
    await day(db, w, fri)
    await db.execute("UPDATE fulltext_passes SET state = 'done' WHERE paper_id = %s", (P.id,))
    await area(db, R, "related")
    await listed(db, mon2, R)
    tree = await taxonomy_store.load(db)
    async with model_run(db, Config(), "digests", gateway=w.gateway) as run:
        await digests.nightly(db, run, tree, mon2)
    assert {p for p, _ in (await shown(db, INTERP.slug, "day", mon2))[1]} == {P.id, R.id}
    assert {p for p, _ in (await shown(db, INTERP.slug, "week", MON))[1]} == {P.id, Q.id}
    await listed(db, date(2026, 10, 19), S)
    await week(db, w, mon2)
    assert {p for p, _ in (await shown(db, INTERP.slug, "week", mon2))[1]} == {R.id}


# -- updated papers --------------------------------------------------------------------------------

MAY = "2026-05-04T15:00:00+00:00"  # first out on Tue 5 May
JUNE = "2026-06-01T15:00:00+00:00"  # first out on Tue 2 June
FOR_TUE = "2026-10-05T15:00:00+00:00"  # Monday 11:00 in New York: Tuesday's listing


def updated(id: str, *later: str, title: str = "Back with a new version", **more):
    """An old paper first out on 5 May, back with versions submitted at `later` (by default
    one in Tuesday's listing); a later time of None is a version without one."""
    found = history(MAY, *(later or (FOR_TUE,)))
    found = [{k: v for k, v in e.items() if v is not None} for e in found]
    return paper(id, title=title, version=len(found), version_history=found, **more)


U = updated("2605.00011")


async def field(conn, p, fit: str) -> None:
    """A member of the Field topic's class, graded there at `fit`, grading finished."""
    await classify(conn, p, AGENTS.slug)
    await analyse(conn, p)
    await grade(conn, p, fit, AGENTS)
    await verdicts.passed(conn, [(p.id, await papers_store.revision(conn, p.id))])


async def digest_row(conn, topic: str, period: str, on: date) -> dict:
    return await (
        await conn.execute(
            "SELECT * FROM digests WHERE topic = %s AND period = %s AND day = %s",
            (topic, period, on),
        )
    ).fetchone()


async def test_an_old_papers_new_version_is_listed_once_in_its_daily_and_its_weekly(db):
    """U, first out in May, is back with v2 in Tuesday's listing: Tuesday's daily lists it as
    updated, after the papers. Its v3, in the next Monday's listing, brings it back in no later
    daily; the week's weekly recaps it once, at the version its daily listed, and the next
    weekly, though v3 is of its week, does not."""
    w = Writer()
    await papers_store.upsert(db, [U])
    await focus(db, U, "central")
    await focus(db, S, "related")
    await listed(db, TUE, S)
    await day(db, w, TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == (
        "written",
        [(S.id, "listed"), (U.id, "updated")],
    )
    tuesday = await digest_row(db, FAITH.slug, "day", TUE)
    assert [p["version"] for p in await store_.papers_of(db, tuesday["id"])] == [None, 2]
    await papers_store.upsert(db, [updated(U.id, FOR_TUE, "2026-10-09T15:00:00+00:00")])
    await focus(db, Q, "related")
    await listed(db, THU, Q)
    await day(db, w, THU)
    assert await shown(db, FAITH.slug, "day", THU) == ("written", [(Q.id, "listed")])
    await week(db, w, MON)
    assert await shown(db, FAITH.slug, "week", MON) == (
        "written",
        [(S.id, "listed"), (Q.id, "listed"), (U.id, "updated")],
    )
    weekly = await digest_row(db, FAITH.slug, "week", MON)
    assert [p["version"] for p in await store_.papers_of(db, weekly["id"])][-1] == 2
    await listed(db, date(2026, 10, 12), R)
    await day(db, w, date(2026, 10, 12))  # v3's own listing day
    assert await shown(db, FAITH.slug, "day", date(2026, 10, 12)) == ("nothing_new", [])
    await week(db, w, date(2026, 10, 12))
    assert await shown(db, FAITH.slug, "week", date(2026, 10, 12)) == ("nothing_new", [])


async def test_updated_papers_add_no_call_and_stay_out_of_the_text_counts_and_rarity(db):
    """U is a must read, rarer than the day's one related paper: the writer is not given it,
    and the digest's counts, best rarity, evaluation input and voice-check titles leave it out."""
    w = Writer()
    await papers_store.upsert(db, [U])
    await focus(db, U, "central")
    await focus(db, S, "related")
    await listed(db, TUE, S)
    await day(db, w, TUE)
    assert len(w.asked) == 1 and U.title not in w.asked[0] and S.title in w.asked[0]
    row = await digest_row(db, FAITH.slug, "day", TUE)
    papers = {p["paper_id"]: p for p in await store_.papers_of(db, row["id"])}
    assert row["counts"] == {
        "papers": 1,
        "picks": 0,
        "central": 0,
        "related": 1,
        "must_read": 0,
        "updated": 1,
    }
    assert row["best_rarity"] == papers[S.id]["rarity"] != papers[U.id]["rarity"]
    assert [d.paper_id for d in await store_.as_shown(db, row["id"], True)] == [S.id]
    assert [r["titles"] for r in await store_.written(db, TUE, TUE)] == [[S.title]]


async def test_an_updated_list_is_by_grade_then_rarity_then_newest_first_out(db):
    """Three must reads, the landmark one rarer, then of two alike the one first out in June
    before May's, then a relevant one, whatever their ids."""
    w = Writer()
    landmark = updated("2605.00013", title="A landmark")
    june = paper(
        "2606.00019", title="Out in June", version=2, version_history=history(JUNE, FOR_TUE)
    )
    solid = updated("2605.00012", title="Solid")
    relevant = updated("2605.00011", title="Related only")
    await papers_store.upsert(db, [landmark, june, solid, relevant])
    for p, fit, weight in (
        (landmark, "central", "landmark"),
        (june, "central", "solid"),
        (solid, "central", "solid"),
        (relevant, "related", "solid"),
    ):
        await classify(db, p, FAITH.slug)
        await analyse(db, p, weight)
        await grade(db, p, fit)
    await listed(db, TUE, S)
    await day(db, w, TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == (
        "nothing_new",
        [
            (landmark.id, "updated"),
            (june.id, "updated"),
            (solid.id, "updated"),
            (relevant.id, "updated"),
        ],
    )


async def test_a_day_with_only_updated_papers_is_nothing_new_and_its_weekly_recaps_them(db):
    """U's v2 is in Friday's listing the week before, graded late: Tuesday's daily, with
    nothing new, lists it without a call, and Tuesday's weekly, though its version is not of
    the week, recaps it."""
    w = Writer()
    u = updated(U.id, "2026-10-01T15:00:00+00:00")
    await papers_store.upsert(db, [u])
    await focus(db, u, "central")
    await listed(db, TUE, S)  # released, nothing due
    await day(db, w, TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == ("nothing_new", [(U.id, "updated")])
    assert w.asked == []
    await week(db, w, MON)
    assert await shown(db, FAITH.slug, "week", MON) == ("nothing_new", [(U.id, "updated")])
    assert (await digest_row(db, FAITH.slug, "week", MON))["counts"]["updated"] == 1


async def test_no_updated_row_for_a_paper_shown_as_new_or_adjacent_or_undated(db):
    w = Writer()
    shown_new = updated("2605.00012", title="Shown in May")
    adjacent = updated("2605.00014", title="Only adjacent")
    undated = updated("2605.00015", None, title="Its v2 has no time")
    await papers_store.upsert(db, [shown_new, adjacent, undated])
    await focus(db, shown_new, "central")
    await day(db, w, date(2026, 5, 5))  # shown as new on its own day
    await focus(db, adjacent, "adjacent")
    await focus(db, undated, "central")
    await focus(db, S, "related")
    await listed(db, TUE, S)
    await day(db, w, TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == ("written", [(S.id, "listed")])


async def test_the_three_weeks_of_an_update_are_exact_on_both_sides(db):
    """For Tuesday's daily, a v2 in Tuesday 15 Sep's listing is three weeks old and one in
    Monday 14 Sep's more. A paper first out on Tue 15 Sep, exactly three weeks before its v2 in
    Tuesday's listing, is not old: graded after Tuesday's daily, it is in Wednesday's neither as
    new nor as updated."""
    w = Writer()
    edge = updated(U.id, "2026-09-14T15:00:00+00:00")
    stale = updated("2605.00012", "2026-09-11T15:00:00+00:00", title="Too long ago")
    young = paper(
        "2609.00016", version=2, version_history=history("2026-09-14T15:00:00+00:00", FOR_TUE)
    )
    await papers_store.upsert(db, [edge, stale, young])
    await focus(db, edge, "central")
    await focus(db, stale, "central")
    await listed(db, TUE, S)
    await day(db, w, TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == ("nothing_new", [(U.id, "updated")])
    await focus(db, young, "central")
    await listed(db, WED, Q)
    await day(db, w, WED)
    assert await shown(db, FAITH.slug, "day", WED) == ("nothing_new", [])


async def test_a_rewritten_version_waits_for_its_grade(db):
    first = paper(U.id, title=U.title, version=1, version_history=history(MAY))
    await papers_store.upsert(db, [first])
    await focus(db, first, "central")
    rewritten = U.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(db, [rewritten])
    await listed(db, TUE, S)
    await day(db, Writer(), TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == ("nothing_new", [])
    await grade(db, rewritten, "central")
    await listed(db, WED, Q)
    await day(db, Writer(), WED)
    assert await shown(db, FAITH.slug, "day", WED) == ("nothing_new", [(U.id, "updated")])


async def test_an_area_lists_an_updated_must_read_once_rechecked_and_no_lesser_one(db):
    w = Writer()
    relevant = updated("2605.00012", title="Only relevant")
    await papers_store.upsert(db, [U, relevant])
    await area(db, U, "central")  # must read: waits for its re-check
    await area(db, relevant, "related")
    await area(db, S, "related")
    await listed(db, TUE, S)
    await day(db, w, TUE)
    assert await shown(db, INTERP.slug, "day", TUE) == ("written", [(S.id, "listed")])
    await db.execute("UPDATE fulltext_passes SET state = 'done' WHERE paper_id = %s", (U.id,))
    await area(db, Q, "related")
    await listed(db, WED, Q)
    await day(db, w, WED)
    assert await shown(db, INTERP.slug, "day", WED) == (
        "written",
        [(Q.id, "listed"), (U.id, "updated")],
    )
    assert U.title not in w.asked[-1]  # no pick candidate, nor in the text


async def test_a_field_weekly_lists_updated_must_reads_and_one_graded_late_in_the_next(db):
    """Both back with versions in Friday's listing: U, graded in time, is in that week's
    weekly; V, graded after it, in the next. A relevant one never is, nor one a weekly showed
    as new in May."""
    w = Writer()
    friday = "2026-10-08T15:00:00+00:00"
    late = updated("2605.00012", friday, title="Graded late")
    relevant = updated("2605.00013", friday, title="Only relevant")
    shown_new = updated("2605.00014", friday, title="Shown in May")
    await papers_store.upsert(db, [updated(U.id, friday), late, relevant, shown_new])
    await field(db, shown_new, "central")
    await week(db, w, date(2026, 5, 4))
    assert (await shown(db, AGENTS.slug, "week", date(2026, 5, 4)))[1] == [(shown_new.id, "pick")]
    await field(db, U, "central")
    await field(db, relevant, "related")
    await week(db, w, MON)
    assert await shown(db, AGENTS.slug, "week", MON) == ("nothing_new", [(U.id, "updated")])
    await field(db, late, "central")
    await week(db, w, date(2026, 10, 12))
    assert await shown(db, AGENTS.slug, "week", date(2026, 10, 12)) == (
        "nothing_new",
        [(late.id, "updated")],
    )


async def test_an_updated_must_read_is_the_pipelines_on_the_current_text(db):
    """Graded must read on its first text, then rewritten, U is not listed while its new text
    has no verdict (its grading ended without one), only once graded must read again. One
    relevant by the pipeline is not listed when the owner's word makes it must read; a landmark
    graded central, which the owner's related leaves must read, is."""
    w = Writer()
    friday = "2026-10-08T15:00:00+00:00"
    first = paper(U.id, title=U.title, version=1, version_history=history(MAY))
    word_only = updated("2605.00012", friday, title="Must read by the owner's word")
    kept = updated("2605.00013", friday, title="A landmark, related by the owner")
    await papers_store.upsert(db, [first, word_only, kept])
    await field(db, first, "central")
    rewritten = updated(U.id, friday, abstract="An entirely different abstract about robots.")
    await papers_store.upsert(db, [rewritten])
    await classify(db, rewritten, AGENTS.slug)
    await analyse(db, rewritten)
    await verdicts.passed(db, [(U.id, 2)])  # grading done for the new text, no verdict
    await field(db, word_only, "related")
    label = Label(
        paper_id=word_only.id, topic=AGENTS.slug, value=LabelValue.CENTRAL, origin="reader"
    )
    await word.label(db, label)
    await classify(db, kept, AGENTS.slug)
    await analyse(db, kept, "landmark")
    await grade(db, kept, "central", AGENTS)
    await verdicts.passed(db, [(kept.id, 1)])
    label = Label(paper_id=kept.id, topic=AGENTS.slug, value=LabelValue.RELATED, origin="reader")
    await word.label(db, label)
    grades = await (
        await db.execute(
            "SELECT paper_id, grade FROM effective_grades WHERE topic = %s", (AGENTS.slug,)
        )
    ).fetchall()
    assert {g["paper_id"]: g["grade"] for g in grades} == {
        U.id: "must_read",
        word_only.id: "must_read",
        kept.id: "must_read",
    }
    await week(db, w, MON)
    assert await shown(db, AGENTS.slug, "week", MON) == ("nothing_new", [(kept.id, "updated")])
    await grade(db, rewritten, "central", AGENTS)
    await week(db, w, date(2026, 10, 12))
    assert await shown(db, AGENTS.slug, "week", date(2026, 10, 12)) == (
        "nothing_new",
        [(U.id, "updated")],
    )


async def test_a_weekly_recaps_a_dailys_updated_paper_no_weekly_has_however_old(db):
    """Listed as updated by the daily of Tue 15 Sep, U is in no weekly while they are not
    written (a pause): the weekly of 5 Oct recaps it."""
    w = Writer()
    u = updated(U.id, "2026-09-14T15:00:00+00:00")
    await papers_store.upsert(db, [u])
    await focus(db, u, "central")
    await listed(db, date(2026, 9, 15), S)
    await day(db, w, date(2026, 9, 15))
    assert (await shown(db, FAITH.slug, "day", date(2026, 9, 15)))[1] == [(U.id, "updated")]
    await week(db, w, MON)
    assert await shown(db, FAITH.slug, "week", MON) == ("nothing_new", [(U.id, "updated")])


async def test_the_monday_after_recaps_last_weeks_versions_and_keeps_its_own(db):
    """Monday 12 Oct's daily, written before the weekly of 5 Oct, lists L, whose v2 was in
    Friday's listing and graded late, and U, whose v2 is in Monday's own: the weekly of 5 Oct
    recaps L, and U waits for the weekly of 12 Oct."""
    w = Writer()
    late = updated("2605.00016", "2026-10-08T15:00:00+00:00", title="Graded late")
    monday = updated(U.id, "2026-10-09T15:00:00+00:00")
    await papers_store.upsert(db, [late, monday])
    await focus(db, late, "central")
    await focus(db, monday, "central")
    await focus(db, S, "related")
    after = MON + timedelta(days=7)
    await listed(db, after, S)
    await day(db, w, after)
    assert (await shown(db, FAITH.slug, "day", after))[1] == [
        (S.id, "listed"),
        (U.id, "updated"),
        (late.id, "updated"),
    ]
    await week(db, w, MON)
    assert (await shown(db, FAITH.slug, "week", MON))[1] == [(late.id, "updated")]
    await week(db, w, after)
    assert (await shown(db, FAITH.slug, "week", after))[1] == [
        (S.id, "listed"),
        (U.id, "updated"),
    ]


async def test_an_updated_paper_is_not_a_focus_weeklys_earlier_paper(db):
    """Listed as updated at Fit central the week before, U is not one the next weekly may
    bring back; P, listed there as new, is."""
    w = Writer()
    u = updated(U.id, "2026-09-28T15:00:00+00:00")  # in Tuesday 29 Sep's listing
    await papers_store.upsert(db, [u])
    await focus(db, u, "central")
    await focus(db, P, "central")
    await listed(db, date(2026, 9, 29), P)
    await day(db, w, date(2026, 9, 29))
    await week(db, w, date(2026, 9, 28))
    assert (await shown(db, FAITH.slug, "week", date(2026, 9, 28)))[1] == [
        (P.id, "listed"),
        (U.id, "updated"),
    ]
    await focus(db, Q, "central")
    await listed(db, TUE, Q)
    await day(db, w, TUE)
    await week(db, w, MON)
    assert P.title in w.asked[-1] and U.title not in w.asked[-1]


async def test_an_updated_row_is_no_cover_for_a_paper_due_as_new(db):
    """Should U's first day move into the window (a guessed day corrected), its updated row on
    Tuesday neither makes it one of the weekly's new papers (the weekly recaps it as updated)
    nor keeps it from a later daily as new."""
    w = Writer()
    await papers_store.upsert(db, [U])
    await focus(db, U, "central")
    await focus(db, S, "related")
    await listed(db, TUE, S)
    await day(db, w, TUE)
    await listed(db, THU, U)
    await week(db, w, MON)
    assert await shown(db, FAITH.slug, "week", MON) == (
        "written",
        [(S.id, "listed"), (U.id, "updated")],
    )
    await day(db, w, THU)
    assert await shown(db, FAITH.slug, "day", THU) == ("written", [(U.id, "listed")])


# -- quiet days and records ------------------------------------------------------------------------


async def test_a_digest_shows_a_papers_rarity_as_a_mixed_list_does(db):
    """A Field topic's must read shows on its page at its grade's level, in a digest lowered by
    the Field's depth offset, as on every mixed list."""
    await classify(db, P, AGENTS.slug)
    await analyse(db, P)
    await grade(db, P, "central", AGENTS)
    await verdicts.passed(db, [(P.id, 1)])
    await listed(db, TUE, P)
    await week(db, Writer(), MON)
    row = await (
        await db.execute(
            """SELECT dp.rarity, r.level, g.rarity AS page
               FROM digest_papers dp JOIN digests d ON d.id = dp.digest_id
               JOIN rarities r ON r.paper_id = dp.paper_id
               JOIN effective_grades g ON g.paper_id = dp.paper_id AND g.topic = d.topic
               WHERE d.topic = %s""",
            (AGENTS.slug,),
        )
    ).fetchone()
    assert row["rarity"] == row["level"] != row["page"]


async def test_a_day_without_a_listing_has_no_digests_and_a_quiet_topic_says_nothing_new(db):
    w = Writer()
    assert not await day(db, w, SAT)
    assert (await (await db.execute("SELECT count(*) AS n FROM digests")).fetchone())["n"] == 0
    await area(db, P, "related")
    await listed(db, TUE, P)
    assert await day(db, w, TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == ("nothing_new", [])
    assert await shown(db, INTERP.slug, "day", TUE) == ("written", [(P.id, "listed")])
    assert len(w.asked) == 1  # nothing new is written without a call


async def test_a_paused_topic_gets_no_digest(db):
    paused = TREE.following(FAITH.model_copy(update={"status": Status.PAUSED}))
    await taxonomy_store.save(db, paused)
    await focus(db, P, "central")
    await listed(db, TUE, P)
    await day(db, Writer(), TUE)
    assert await shown(db, FAITH.slug, "day", TUE) == (None, [])


async def test_a_published_digest_is_a_record_but_for_the_set_aside_mark(db):
    w = Writer()
    await focus(db, P, "central")
    await listed(db, TUE, P)
    await day(db, w, TUE)
    before = await db.execute("SELECT * FROM digests WHERE topic = %s", (FAITH.slug,))
    before = await before.fetchone()
    # The owner sets P aside; a re-run of the day rewrites nothing.
    label = Label(paper_id=P.id, topic=FAITH.slug, value=LabelValue.ADJACENT, origin="reader")
    await word.label(db, label)
    await day(db, w, TUE)
    after = await (
        await db.execute("SELECT * FROM digests WHERE topic = %s", (FAITH.slug,))
    ).fetchone()
    assert after == before and len(w.asked) == 1
    [shown_p] = await store_.papers_of(db, before["id"])
    assert (shown_p["fit"], shown_p["grade"], shown_p["set_aside"]) == (
        "central",
        "must_read",
        True,
    )


async def test_a_published_digest_keeps_its_titles_and_its_set_aside_mark(db):
    """The set-aside mark is the owner's word in force, so a later verdict that agrees with it
    keeps it; a title arXiv rewrites later stays as the digest showed it."""
    await focus(db, P, "central")
    await listed(db, TUE, P)
    await day(db, Writer(), TUE)
    label = Label(paper_id=P.id, topic=FAITH.slug, value=LabelValue.ADJACENT, origin="reader")
    await word.label(db, label)
    await grade(db, P, "adjacent")
    await db.execute("UPDATE papers SET title = 'A new title' WHERE id = %s", (P.id,))
    row = await (
        await db.execute("SELECT id FROM digests WHERE topic = %s", (FAITH.slug,))
    ).fetchone()
    [shown_p] = await store_.papers_of(db, row["id"])
    assert (shown_p["title"], shown_p["set_aside"]) == (P.title, True)


async def test_publishing_a_digest_emits_its_event_and_a_failed_one_none(db):
    w = Writer()
    w.failures = [ProviderError("down")]
    await focus(db, P, "central")
    await listed(db, TUE, P)
    await day(db, w, TUE)  # Focus fails, Area says nothing new
    await day(db, w, TUE)  # Focus written again on the retry; Area stays as it is
    rows = await (
        await db.execute("SELECT paper_id, payload FROM outbox WHERE type = 'digest.written'")
    ).fetchall()
    assert sorted((r["payload"]["topic"], r["payload"]["state"]) for r in rows) == [
        (FAITH.slug, "written"),
        (INTERP.slug, "nothing_new"),
    ]
    assert {r["paper_id"] for r in rows} == {None}
    assert all(r["payload"]["day"] == str(TUE) for r in rows)


async def test_a_day_stays_released_when_its_papers_are_dismissed(db):
    """Dismissing stops processing; it does not undo arXiv's listing."""
    await listed(db, TUE, P)
    await db.execute("UPDATE papers SET dismissed_at = now() WHERE id = %s", (P.id,))
    assert await store_.released(db, TUE)
    assert await store_.released_days(db, MON, WED) == {TUE}


# -- failed digests --------------------------------------------------------------------------------


async def test_a_failed_digest_keeps_its_papers_due_and_the_next_run_writes_it(db):
    w = Writer()
    w.failures = [ProviderError("down")]
    await focus(db, P, "central")
    await listed(db, TUE, P)
    await day(db, w, TUE)
    assert (await shown(db, FAITH.slug, "day", TUE))[0] == "failed"
    assert await store_.count_failed(db) == 1
    # Wednesday's run writes Tuesday's again first, so Wednesday's does not take P.
    await focus(db, Q, "central")
    await listed(db, WED, Q)
    tree = await taxonomy_store.load(db)
    async with model_run(db, Config(), "digests", gateway=w.gateway) as run:
        await digests.nightly(db, run, tree, WED)
    assert await shown(db, FAITH.slug, "day", TUE) == ("written", [(P.id, "listed")])
    assert await shown(db, FAITH.slug, "day", WED) == ("written", [(Q.id, "listed")])
    assert await store_.count_failed(db) == 0


async def test_a_window_writes_a_failed_digest_again_before_its_days(db):
    """A failed Tuesday digest is written again before a recovery window's Wednesday, so
    Wednesday's does not take its paper."""
    w = Writer()
    w.failures = [ProviderError("down")]
    await focus(db, P, "central")
    await listed(db, TUE, P)
    await day(db, w, TUE)
    assert (await shown(db, FAITH.slug, "day", TUE))[0] == "failed"
    await focus(db, Q, "central")
    await listed(db, WED, Q)
    await digests_job.window(db, Config(), WED, WED, gateway=w.gateway)
    assert await shown(db, FAITH.slug, "day", TUE) == ("written", [(P.id, "listed")])
    assert await shown(db, FAITH.slug, "day", WED) == ("written", [(Q.id, "listed")])


async def test_a_failed_digest_is_on_the_status_line_until_its_topic_pauses(db):
    w = Writer()
    w.failures = [ProviderError("down")]
    await focus(db, P, "central")
    await listed(db, TUE, P)
    await day(db, w, TUE)
    await runs.finish(db, await runs.start(db, "nightly"))
    status = await reading.status(db)
    assert (status["state"], status["digests_failed"]) == ("attention", 1)
    await taxonomy_store.save(
        db, TREE.following(FAITH.model_copy(update={"status": Status.PAUSED}))
    )
    status = await reading.status(db)
    assert (status["state"], status["digests_failed"]) == ("quiet", 0)
    tree = await taxonomy_store.load(db)
    async with model_run(db, Config(), "digests", gateway=w.gateway) as run:
        await digests.retry_failed(db, run, tree)
    assert (await shown(db, FAITH.slug, "day", TUE))[0] == "failed"  # not written while paused


async def test_a_topics_failed_digests_step_down_the_writers_list_one_model_each(db):
    # models.md, roles: an unusable answer moves the next attempt one model down, per failed
    # digest of the topic; no answer moves nothing; the last model is kept.
    class Stepped(Writer):
        def __init__(self, script: list[Exception | Reply | str | None]) -> None:
            super().__init__()
            self.script, self.models = script, []

        def respond(self, role, request) -> str | Reply:
            self.models.append(role.model)
            said = self.script.pop(0) if self.script else None
            if isinstance(said, Exception):
                raise said
            return said if said is not None else super().respond(role, request)

    opus, sonnet, kimi = [m.model for m in Config().models.writer.models]
    # Kimi refusing goes on to Opus, at the top, whose unusable answer still keeps Kimi next.
    kimi_refuses = Reply("", Stop.REFUSED, Usage(model=kimi), refusal="bio")
    w = Stepped(["{not json", ProviderError("down"), "{not json", kimi_refuses, "{not json"])
    await focus(db, P, "central")
    await listed(db, TUE, P)
    await day(db, w, TUE)
    tree = await taxonomy_store.load(db)
    for _ in range(4):
        async with model_run(db, Config(), "digests", gateway=w.gateway) as run:
            await digests.retry_failed(db, run, tree)
    assert w.models == [opus, sonnet, sonnet, kimi, opus, kimi]
    assert (await shown(db, FAITH.slug, "day", TUE))[0] == "written"
    found = await db.execute("SELECT model FROM digests WHERE topic = %s", (FAITH.slug,))
    assert (await found.fetchone())["model"] == Config().models.writer.models[2].label
    # A written digest puts the topic's next one back on the role's own model.
    await focus(db, Q, "central")
    await listed(db, WED, Q)
    await day(db, w, WED)
    assert w.models[-1] == opus


async def test_an_answer_naming_a_paper_that_is_not_a_candidate_fails_the_digest(db):
    class Wrong(Writer):
        def respond(self, role, request) -> str:
            answer = json.loads(super().respond(role, request))
            answer["picks"] = [{"paper": "2610.99999", "why": "Made up."}]
            return json.dumps(answer)

    await area(db, P, "central")
    await db.execute("UPDATE fulltext_passes SET state = 'done' WHERE paper_id = %s", (P.id,))
    await listed(db, TUE, P)
    await day(db, Wrong(), TUE)
    assert (await shown(db, INTERP.slug, "day", TUE))[0] == "failed"


# -- the bounded wait ------------------------------------------------------------------------------


class Archive:
    async def aclose(self) -> None:
        pass


async def test_the_wait_ends_when_the_passes_it_waits_for_finish(db, monkeypatch):
    await focus(db, P, "central")  # queues P's Brief
    since = datetime.now(UTC) - timedelta(minutes=1)
    worked = []

    async def work(conn, config, archive, run=None, limit=None, deadline=None):
        worked.append(deadline)
        if len(worked) == 2:
            await conn.execute("UPDATE fulltext_passes SET state = 'done'")
        return {}

    monkeypatch.setattr(fulltext, "archive_for", lambda config: Archive())
    monkeypatch.setattr(fulltext, "work", work)
    deadline = nightly.time.monotonic() + 60
    async with model_run(db, Config(), "nightly", gateway=Writer().gateway) as run:
        assert await store_.waiting_passes(db, since) == 1
        await nightly.wait_for_texts(db, Config(), run, since, deadline, 0.01)
    assert worked == [deadline, deadline] and "digest_wait_ended" not in run.counts


async def test_the_wait_is_bounded_and_a_failed_pass_ends_it(db, monkeypatch):
    await focus(db, P, "central")
    since = datetime.now(UTC) - timedelta(minutes=1)

    async def work(conn, config, archive, run=None, limit=None, deadline=None):
        return {}

    monkeypatch.setattr(fulltext, "archive_for", lambda config: Archive())
    monkeypatch.setattr(fulltext, "work", work)
    async with model_run(db, Config(), "nightly", gateway=Writer().gateway) as run:
        deadline = nightly.time.monotonic() + 0.06
        wait = nightly.wait_for_texts(db, Config(), run, since, deadline, 0.01)
        await asyncio.wait_for(wait, 5)  # the pass never finishes: only the bound ends it
    assert run.counts["digest_wait_ended"] == 1
    # A pass that failed in this run is not waited for: it is tried on the next one.
    await db.execute("UPDATE fulltext_passes SET state = 'failed', failed_runs = 1")
    assert await store_.waiting_passes(db, since) == 0


async def test_the_wait_works_no_more_passes_than_the_runs_limit_leaves(db, monkeypatch):
    """`nightly --limit`: the passes worked before the wait count against it too."""
    await focus(db, P, "central")
    since = datetime.now(UTC) - timedelta(minutes=1)
    limits = []

    async def work(conn, config, archive, run=None, limit=None, deadline=None):
        limits.append(limit)
        return Counter(briefs_written=1)

    monkeypatch.setattr(fulltext, "archive_for", lambda config: Archive())
    monkeypatch.setattr(fulltext, "work", work)
    async with model_run(db, Config(), "nightly", gateway=Writer().gateway) as run:
        deadline = nightly.time.monotonic() + 0.1
        await nightly.wait_for_texts(db, Config(), run, since, deadline, 0.01, limit=2)
    assert limits == [2, 1]  # then the allowance is spent and the wait only waits


async def test_a_nightly_run_past_midnight_writes_the_digests_of_the_day_it_started(
    db, monkeypatch
):
    """Started Monday 23:59 in the schedule's zone and ending after midnight, the run writes
    Monday's dailies and the week before's weeklies."""
    starts = iter([datetime(2026, 10, 6, 3, 59, tzinfo=UTC)])  # Monday 23:59 in New York

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            at = next(starts, datetime(2026, 10, 6, 5, 0, tzinfo=UTC))  # then Tuesday 01:00
            return at.astimezone(tz) if tz else at

    monkeypatch.setattr(nightly, "datetime", Clock)
    await focus(db, P, "central")
    await verdicts.passed(db, [(P.id, 1)])
    await db.execute("UPDATE papers SET dismissed_at = now() WHERE id <> %s", (P.id,))  # no calls
    await listed(db, MON, P)
    config = Config.model_validate({"schedule": {"timezone": "America/New_York"}})
    await nightly.run(db, config, fetch=False, gateway=Writer().gateway)
    assert await shown(db, FAITH.slug, "day", MON) == ("written", [(P.id, "listed")])
    assert (await shown(db, FAITH.slug, "week", MON - timedelta(days=7)))[0] == "nothing_new"


async def test_a_nightly_run_without_digests_writes_none(db):
    """`nightly --no-digests` (the restart, whose digests `digests write` makes for its window):
    on a Monday with a paper listed, neither the day's dailies nor last week's weeklies."""
    await focus(db, P, "central")
    await verdicts.passed(db, [(P.id, 1)])
    await db.execute("UPDATE papers SET dismissed_at = now() WHERE id <> %s", (P.id,))  # no calls
    await listed(db, MON, P)
    await nightly.run(
        db, Config(), fetch=False, gateway=Writer().gateway, today=MON, with_digests=False
    )
    assert (await (await db.execute("SELECT count(*) AS n FROM digests")).fetchone())["n"] == 0


async def test_no_batch_of_passes_starts_after_the_deadline(db):
    """The full-text work before the digests stops at the wait's deadline; the worker takes
    the passes left."""
    await focus(db, P, "central")
    await db.execute("UPDATE fulltext_passes SET state = 'ready'")
    past = nightly.time.monotonic() - 1
    assert await fulltext.work(db, Config(), None, deadline=past) == {}
    state = await (await db.execute("SELECT state, tried_at FROM fulltext_passes")).fetchone()
    assert (state["state"], state["tried_at"]) == ("ready", None)


# -- the voice check -------------------------------------------------------------------------------


def test_the_voice_check_names_each_rule_broken_and_spares_titles():
    assert voice_problems("Required reading for agent builders: you will like it.") == [
        "addresses the reader",
        "names an audience",
        "advises reading",
    ]
    assert voice_problems("It is a must read.") == ["names a grade"]
    text = "Attention Is All You Need set the architecture the papers build on."
    assert voice_problems(text, ["Attention Is All You Need"]) == []
    shortened = "Passing the Test You Trained On finds rankings transfer poorly."
    assert voice_problems(shortened, ["Passing the Test You Trained On: Detector Rankings"]) == []
    question = "Do You Need a Bigger Probe? asks whether width or depth carries the signal."
    assert voice_problems(question, ["Do You Need a Bigger Probe? Width Versus Depth"]) == []
    marked_title = "[Is It You?](2610.00001) finds a probe."
    assert voice_problems(marked_title, ["Is It You? Probes for the Reader"]) == []
    assert voice_problems("[a toolkit for developers](2610.00001) ships.") == ["names an audience"]


# -- the papers a text marks -----------------------------------------------------------------------

TEXT = " ".join(["Probes recover hidden steps."] * 20)


def answer(model, text: str, picks=(), gist: str = "words " * 40):
    return model.model_validate_json(
        json.dumps({"text": text, "gist": gist, "picks": [{"paper": p, "why": "."} for p in picks]})
    )


def test_a_digest_mentions_the_papers_its_text_marks_besides_its_picks():
    area = writer._answer(
        writer.Picked,
        text_words=writer.TEXT_WORDS["area_day"],
        papers=frozenset({P.id, Q.id, R.id}),
        candidates=frozenset({P.id}),
        picks_range=(1, 1),
    )
    text = (
        f"[Traces]({R.id}) and [Hidden steps]({P.id}) agree; [Traces]({R.id}) adds "
        f"[Learning with [MASK] tokens]({Q.id}). {TEXT}"
    )
    out = answer(area, text, picks=[P.id], gist=f"[Traces]({R.id}) " + "words " * 40)
    assert out.mentioned == [R.id, Q.id]  # once each, in order, without the pick
    assert out.gist.startswith("Traces words")  # a Gist names papers without marks
    assert plain(out.text).startswith("Traces and Hidden steps agree; Traces adds")
    for made_up in ("[Made up](2610.99999)", "[A [bracketed] one](2610.99999)"):
        with pytest.raises(ValueError, match="not one of the digest's"):
            answer(area, f"{made_up} {TEXT}", picks=[P.id])
    assert plain("[the code](https://github.com/x/y), [old](cs/9901001)") == (
        "[the code](https://github.com/x/y), old"  # only an arXiv id makes a mark
    )


def test_a_focus_weekly_brings_back_the_one_earlier_paper_its_text_marks():
    weekly = writer._answer(
        writer.FocusWeek, papers=frozenset({P.id, Q.id, R.id}), earlier=frozenset({Q.id, R.id})
    )
    assert answer(weekly, f"[This week's]({P.id}) {TEXT}").recalled is None
    assert answer(weekly, f"[This week's]({P.id}) and [back]({Q.id}) {TEXT}").recalled == Q.id
    with pytest.raises(ValueError, match="more than one"):
        answer(weekly, f"[One [a]]({Q.id}) and [two [b]]({R.id}) {TEXT}")


# -- the Digest tab --------------------------------------------------------------------------------


def client(conn, w: Writer, config: Config | None = None) -> httpx.AsyncClient:
    dsn = f"{conn.test_dsn}?options=-csearch_path%3D{conn.test_schema}"
    app = create_app(dsn, TOKEN, config or Config(), gateway=w.gateway)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


async def test_the_digest_tab_reads_days_weeks_history_and_a_digest(db):
    w = Writer()
    await focus(db, P, "central")
    await area(db, Q, "related")
    await listed(db, TUE, P, Q)
    await day(db, w, TUE)
    await week(db, w, MON)
    async with client(db, w) as http:
        today = (await http.get(f"/api/digests/days/{TUE}")).json()
        assert today["released"] and [d["topic"]["slug"] for d in today["digests"]] == [
            FAITH.slug,
            INTERP.slug,
        ]
        card = today["digests"][0]
        assert (card["state"], card["counts"]["central"], card["more"]) == ("written", 1, 0)
        assert [p["id"] for p in card["papers"]] == [P.id]
        assert (today["previous"], today["next"]) == (None, None)
        sat = (await http.get(f"/api/digests/days/{SAT}")).json()
        assert (sat["released"], sat["digests"], sat["previous"]) == (False, [], str(TUE))
        monday = (await http.get(f"/api/digests/days/{date(2026, 10, 12)}")).json()
        assert monday["weekly"] == str(MON)
        weekly = (await http.get(f"/api/digests/weeks/{THU}")).json()
        assert weekly["monday"] == str(MON)
        assert [d["topic"]["slug"] for d in weekly["digests"]] == [
            FAITH.slug,
            INTERP.slug,
            AGENTS.slug,
        ]
        history = (await http.get("/api/digests", params={"since": MON, "until": SAT})).json()
        assert [d["day"] for d in history["days"]][:2] == [str(SAT), str(date(2026, 10, 9))]
        assert [len(d["marks"]) for d in history["days"] if d["day"] == str(TUE)] == [2]
        assert [len(x["marks"]) for x in history["weeks"]] == [3]
        assert history["start"] == str(MON)
        wide = {"since": "2026-01-01", "until": str(SAT)}
        assert (await http.get("/api/digests", params=wide)).status_code == 422
        full = (await http.get(f"/api/digests/{card['id']}")).json()
        assert full["text"] and [p["role"] for p in full["papers"]] == ["listed"]
        topics = (await http.get("/api/topics")).json()
        assert [len(t["strip"]) for g in topics for t in g["topics"]] == [1, 1, 1]
        assert (await http.get("/api/digests/999999")).status_code == 404


async def test_the_tab_counts_updated_papers_apart_and_gives_each_its_version(db):
    """A day whose only paper is U, updated at Fit central: its card shows none of its papers
    (they are picks or central papers listed as new) and counts it apart; the digest gives
    its version and when it first came out."""
    w = Writer()
    await papers_store.upsert(db, [U])
    await focus(db, U, "central")
    await listed(db, TUE, S)
    await day(db, w, TUE)
    async with client(db, w) as http:
        [card, _] = (await http.get(f"/api/digests/days/{TUE}")).json()["digests"]
        assert (card["state"], card["papers"], card["more"]) == ("nothing_new", [], 0)
        assert (card["counts"]["papers"], card["counts"]["updated"]) == (0, 1)
        history = (await http.get("/api/digests", params={"since": TUE, "until": TUE})).json()
        assert history["days"][0]["marks"][0]["counts"]["updated"] == 1
        [row] = (await http.get(f"/api/digests/{card['id']}")).json()["papers"]
    assert (row["id"], row["role"], row["version"], row["announced"]) == (
        U.id,
        "updated",
        2,
        "2026-05-05",
    )


async def test_the_tab_steps_only_through_the_digests_it_shows(db):
    """An unfollowed topic keeps its digests, but the tab neither shows nor steps to them."""
    w = Writer()
    tree = await taxonomy_store.load(db)
    async with model_run(db, Config(), "digests", gateway=w.gateway) as run:
        await digests.write(db, run, tree, tree.topic(FAITH.slug), DigestPeriod.DAY, TUE)
        await digests.write(db, run, tree, tree.topic(INTERP.slug), DigestPeriod.DAY, WED)
    await taxonomy_store.save(db, TREE.model_copy(update={"topics": [INTERP, AGENTS]}))
    async with client(db, w) as http:
        wed = (await http.get(f"/api/digests/days/{WED}")).json()
        history = (await http.get("/api/digests", params={"since": MON, "until": SAT})).json()
    assert (wed["previous"], history["start"]) == (None, str(WED))


SCHEDULED = Config(schedule=Schedule(enabled=True))  # the jobs worker runs
AFTERNOON = datetime(2026, 10, 7, 14, 0, tzinfo=SCHEDULED.schedule.zone)  # no nightly due


async def failed_day(db, w: Writer) -> int:
    """P's daily digest of Tuesday, failed: its id."""
    w.failures = [ProviderError("down")]
    await focus(db, P, "central")
    await listed(db, TUE, P)
    await day(db, w, TUE)
    (row,) = await store_.failed(db)
    return row["id"]


async def test_a_failed_digest_written_again_queues_behind_a_held_lock_then_is_written(db):
    """Write again queues a job instead of answering 409 while another job holds the run lock;
    the worker writes the digest once the lock is free, and its Needs you item and its job
    settle together."""
    w = Writer()
    digest_id = await failed_day(db, w)
    async with client(db, w, SCHEDULED) as http:
        quality = (await http.get("/api/settings/quality")).json()
        assert [d["id"] for d in quality["night"]["digests"]] == [digest_id]
        async with await store.sibling(db) as other, run_lock(other):
            asked = await http.post(f"/api/digests/{digest_id}/write")
            assert asked.status_code == 202
            body = asked.json()
            assert (body["state"], body["job"]["state"]) == ("failed", "queued")
            # Asked again (a lost reply, a second click), the same job answers.
            again = (await http.post(f"/api/digests/{digest_id}/write")).json()
            assert again["job"]["id"] == body["job"]["id"]
            # The lock is held: no chunk starts, and the job waits in the queue.
            assert not await worker.Worker(db, SCHEDULED, w.gateway).step(AFTERNOON)
            jobs = (await http.get("/api/jobs")).json()
            [row] = jobs["running"]
            assert (row["kind"], row["state"]) == ("digest", "queued")
            assert row["digest"]["id"] == digest_id
            assert (row["topic"]["slug"], row["digest"]["period"]) == (FAITH.slug, "day")
        assert await worker.Worker(db, SCHEDULED, w.gateway).step(AFTERNOON)
        written = (await http.get(f"/api/digests/{digest_id}")).json()
        assert (written["state"], written["job"]["state"]) == ("written", "done")
        assert [p["id"] for p in written["papers"]] == [P.id]
        quality = (await http.get("/api/settings/quality")).json()
        assert quality["night"]["digests"] == []
        jobs = (await http.get("/api/jobs")).json()
        assert [(j["kind"], j["state"]) for j in jobs["done"]] == [("digest", "done")]
        # Written, it is no longer one to write again.
        assert (await http.post(f"/api/digests/{digest_id}/write")).status_code == 409
    job_runs = await rows(db, "SELECT kind, job_id FROM runs WHERE job_id IS NOT NULL")
    assert [r["kind"] for r in job_runs] == ["job"]  # its cost reaches the month's spend


async def test_a_digest_that_fails_again_fails_its_job_and_both_settle_when_a_run_writes_it(db):
    w = Writer()
    digest_id = await failed_day(db, w)
    async with client(db, w, SCHEDULED) as http:
        w.failures = [ProviderError("still down")] * 4  # every writer of the list
        assert (await http.post(f"/api/digests/{digest_id}/write")).status_code == 202
        assert await worker.Worker(db, SCHEDULED, w.gateway).step(AFTERNOON)
        failed = (await http.get(f"/api/digests/{digest_id}")).json()
        assert (failed["state"], failed["job"]["state"]) == ("failed", "failed")
        assert failed["job"]["error"].startswith("the writer failed again")
        [row] = (await http.get("/api/jobs")).json()["failed"]
        assert row["actions"] == ["again"]
        # The next run writes it: the failed job settles with it, done.
        w.failures = []
        tree = await taxonomy_store.load(db)
        async with model_run(db, Config(), "digests", gateway=w.gateway) as run:
            await digests.retry_failed(db, run, tree)
        settled = (await http.get(f"/api/digests/{digest_id}")).json()
        assert (settled["state"], settled["job"]["state"]) == ("written", "done")
        assert (await http.get("/api/jobs")).json()["failed"] == []


async def test_a_digest_is_written_again_only_through_a_worker_and_while_its_topic_is_active(db):
    w = Writer()
    digest_id = await failed_day(db, w)
    async with client(db, w) as http:  # the schedule off: no worker runs jobs
        off = await http.post(f"/api/digests/{digest_id}/write")
        assert off.status_code == 409 and "schedule is off" in off.json()["detail"]
    paused = FAITH.model_copy(update={"status": Status.PAUSED})
    await taxonomy_store.save(db, TREE.model_copy(update={"topics": [paused, INTERP, AGENTS]}))
    async with client(db, w, SCHEDULED) as http:
        refused = await http.post(f"/api/digests/{digest_id}/write")
        assert refused.status_code == 409 and "not one to write again" in refused.json()["detail"]
    assert await rows(db, "SELECT id FROM jobs") == []


async def rows(conn, query: str, *params) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


DENVER = Schedule(enabled=True, timezone="America/Denver")  # nightly 02:00, deadline 07:30


def denver(hour: int, minute: int = 0, day: date = WED) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=DENVER.zone)


def run(state: str, started: datetime, **more) -> dict:
    return {"state": state, "started_at": started.astimezone(UTC), "papers": 212, **more}


@pytest.mark.parametrize(
    ("now", "latest", "state"),
    [
        (denver(1), None, "waiting"),
        (denver(2, 10), run("quiet", denver(2, 0, TUE)), "waiting"),  # yesterday's run
        (denver(2, 20), run("quiet", denver(2, 0, TUE)), "late"),
        (denver(3), run("running", denver(2), waiting=268), "running"),
        (denver(8), run("running", denver(2), waiting=12), "overrun"),
        (denver(4), run("failed", denver(2), error="anthropic down"), "failed"),
        (denver(9), run("attention", denver(2)), "done"),
        (denver(9), run("quiet", denver(2)), "done"),
        (denver(1), run("quiet", denver(0, 30)), "waiting"),  # by hand before the fire
        (denver(1), run("running", denver(0, 30), waiting=268), "running"),
        (denver(11), run("quiet", denver(10)), "done"),  # again by hand after a failure
    ],
)
def test_todays_run_waits_runs_and_ends_or_is_late(now, latest, state):
    got = today_run(WED, now, DENVER, latest)
    assert got is not None and got.state == state
    assert (got.due, got.deadline) == (denver(2), denver(7, 30))
    if state in ("running", "overrun"):
        assert (got.papers, got.total) == (212, 480 if state == "running" else 224)
    assert got.error == ("anthropic down" if state == "failed" else None)


def test_only_today_has_a_run_and_only_with_the_schedule_on():
    assert today_run(TUE, denver(9), DENVER, None) is None  # yesterday, in Denver
    assert today_run(WED, denver(9), Schedule(timezone="America/Denver"), None) is None
    # 23:30 in Denver is already the next day in UTC: still Wednesday's run
    assert today_run(WED, denver(23, 30), DENVER, None).state == "late"


async def test_the_day_carries_todays_run_on_today_only(db):
    w = Writer()
    config = Config(schedule=Schedule(enabled=True))
    today = datetime.now(config.schedule.zone).date()
    async with client(db, w, config) as http:
        now = (await http.get(f"/api/digests/days/{today}")).json()
        before = (await http.get(f"/api/digests/days/{today - timedelta(days=1)}")).json()
    assert now["run"]["state"] in ("waiting", "late") and now["run"]["due"].startswith(str(today))
    assert before["run"] is None
