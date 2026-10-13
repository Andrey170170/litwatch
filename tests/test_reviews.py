"""Reviews of Radar and Pulse topics, Watch recaps, and the weekly issue."""

import json
import re
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from litwatch.api import create_app
from litwatch.config import Config
from litwatch.core import (
    Cadence,
    Grade,
    Label,
    LabelOrigin,
    LabelValue,
    Topic,
    TopicKind,
    Verdict,
)
from litwatch.feedback import word
from litwatch.jobs import digest, reviews
from litwatch.jobs.context import model_run
from litwatch.llm import Gateway, ProviderError
from litwatch.review import reviews as review_calls
from litwatch.store import papers as papers_store
from litwatch.store import reading as reading_store
from litwatch.store import reviews as reviews_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts
from tests.conftest import AGENTS, FAITH, INTERP, paper
from tests.fakes import ScriptedProvider

TOKEN = "s3cret"
DAILY = INTERP.model_copy(update={"cadence": Cadence.DAILY})
FIELD = [paper(f"2610.0050{i}", title=f"Field paper {i}") for i in range(7)]
P = paper("2610.00001", title="Traces that hide steps")


class Models:
    def __init__(self):
        self.down = False
        self.lede_down = False
        self.shortlist_empty = False
        self.calls: list[str] = []
        self.reviewed: list[int] = []  # how many papers each final review call saw

    def respond(self, role, request):
        if self.down:
            raise ProviderError("InternalServerError")
        ids = re.findall(r"^\[([^\]]+)\]", request.user, re.MULTILINE)
        if "choose up to" in request.system:
            self.calls.append("shortlist")
            return json.dumps({"ids": [] if self.shortlist_empty else ids[:2]})
        if "You review the new arXiv papers" in request.system:
            self.calls.append("review")
            self.reviewed.append(len(ids))
            picks = [{"id": i, "why": f"why {i}"} for i in ids] + [{"id": "9999.9", "why": "?"}]
            return json.dumps({"text": f"A quiet field of {len(ids)}.", "picks": picks})
        if "recap" in request.system:
            self.calls.append("recap")
            return json.dumps({"recap": "The week was about hidden steps."})
        if "lede" in request.system:
            self.calls.append("lede")
            if self.lede_down:
                raise ProviderError("InternalServerError")
            return json.dumps({"lede": "Lede."})
        self.calls.append("take")
        return json.dumps({"take": "A take."})

    def gateway(self, config, journal):
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=())


@pytest.fixture
async def db(conn):
    for topic in (FAITH, DAILY, AGENTS):
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [*FIELD, P])
    for p in FIELD:
        await conn.execute(
            "INSERT INTO memberships (paper_id, topic, topic_revision) VALUES (%s, %s, 1), "
            "(%s, %s, 1)",
            (p.id, DAILY.slug, p.id, AGENTS.slug),
        )
    return conn


async def job(conn, models, work):
    async with model_run(conn, Config(), "nightly", gateway=models.gateway) as run:
        return await work(run)


def test_only_a_radar_topic_can_be_reviewed_daily():
    with pytest.raises(ValueError, match="only a Radar topic"):
        Topic.model_validate(AGENTS.model_dump() | {"cadence": "daily"})
    assert DAILY.kind == TopicKind.RADAR


async def test_a_daily_review_picks_from_its_members_and_makes_an_issue_alone(db):
    models = Models()
    today = Config().schedule.today()
    await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run))
    issue_id = await job(db, models, lambda run: digest.build_daily(db, Config(), run))
    assert issue_id is not None  # no Watch entry, but a review
    review = await (await db.execute("SELECT * FROM reviews")).fetchone()
    assert (review["period"], review["day"], review["state"]) == ("day", today, "written")
    picks = await (await db.execute("SELECT paper_id FROM review_picks")).fetchall()
    assert len(picks) == 5  # at most five a day, and never a paper outside the list
    assert "lede" in models.calls


async def test_a_quiet_day_makes_no_issue_and_a_failed_review_is_tried_again(db):
    models = Models()
    await db.execute("DELETE FROM memberships")
    await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run))
    assert await job(db, models, lambda run: digest.build_daily(db, Config(), run)) is None
    (row,) = await (await db.execute("SELECT state FROM reviews")).fetchall()
    assert row["state"] == "quiet"

    await db.execute(
        "INSERT INTO memberships (paper_id, topic, topic_revision) VALUES (%s, %s, 1)",
        (FIELD[0].id, DAILY.slug),
    )
    models.down = True
    await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run))
    (row,) = await (await db.execute("SELECT state FROM reviews")).fetchall()
    assert row["state"] == "failed"
    # A failed review makes an issue, so the reader sees that it failed.
    assert await job(db, models, lambda run: digest.build_daily(db, Config(), run)) is not None
    models.down = False
    await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run))
    (row,) = await (await db.execute("SELECT state FROM reviews")).fetchall()
    assert row["state"] == "written"


async def test_a_large_field_is_shortlisted_before_it_is_reviewed(db, monkeypatch):
    monkeypatch.setattr(review_calls, "FULL_REVIEW_LIMIT", 3)
    monkeypatch.setattr(review_calls, "CHUNK", 3)
    models = Models()
    await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run))
    assert models.calls.count("shortlist") == 3 and models.calls.count("review") == 1
    # Each chunk keeps its share, so the final call stays within the limit.
    assert models.reviewed == [3]


async def test_the_weekly_issue_reviews_every_field_and_recaps_every_watch_topic(db):
    models = Models()
    schedule = Config().schedule
    monday = schedule.week_of(schedule.today())
    # A paper announced for the Watch topic this week.
    await verdicts.add(
        db, Verdict(paper_id=P.id, topic=FAITH.slug, topic_revision=1, grade=Grade.RELEVANT)
    )
    await word.settle(db, P.id)
    await job(db, models, lambda run: digest.write_takes(db, Config(), run))
    await job(db, models, lambda run: digest.build_daily(db, Config(), run))
    # The owner sets it aside, then changes their mind: the recap still lists the set-aside.
    for value in (LabelValue.NOT_RELEVANT, LabelValue.RELEVANT):
        await word.label(
            db,
            Label(paper_id=P.id, topic=FAITH.slug, value=value, origin=LabelOrigin.READER),
        )
    # A paper whose authors are a JSON null is still shown in a review's picks.
    await db.execute("UPDATE papers SET authors = 'null'::jsonb WHERE id = %s", (FIELD[0].id,))

    issue_id = await job(db, models, lambda run: reviews.weekly(db, Config(), run, monday))
    rows = await (
        await db.execute("SELECT topic, state FROM reviews WHERE period = 'week'")
    ).fetchall()
    assert {(r["topic"], r["state"]) for r in rows} == {
        (FAITH.slug, "written"),
        (DAILY.slug, "written"),  # a daily topic is reviewed weekly too
        (AGENTS.slug, "written"),
    }
    agents_picks = await (
        await db.execute(
            "SELECT count(*) AS n FROM review_picks k JOIN reviews r ON r.id = k.review_id "
            "WHERE r.topic = %s",
            (AGENTS.slug,),
        )
    ).fetchone()
    assert agents_picks["n"] == 3  # a Pulse review picks up to three

    dsn = f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"
    app = create_app(dsn, TOKEN, Config())
    headers = {"authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        issue = (await c.get(f"/api/issues/{issue_id}")).json()
        assert issue["kind"] == "weekly" and issue["lede"] == "Lede."
        faith = next(s for s in issue["sections"] if s["topic"]["slug"] == FAITH.slug)
        assert faith["recap"]["text"] == "The week was about hidden steps."
        assert [p["paper_id"] for p in faith["recap"]["papers"]] == [P.id]
        assert [p["paper_id"] for p in faith["recap"]["set_aside"]] == [P.id]
        # A thumb on the paper for the Watch topic shows on every list that names it.
        await c.put(f"/api/reactions/{P.id}", json={"topic": FAITH.slug, "value": "up"})
        faith = next(
            s
            for s in (await c.get(f"/api/issues/{issue_id}")).json()["sections"]
            if s["topic"]["slug"] == FAITH.slug
        )
        assert faith["recap"]["papers"][0]["reaction"] == "up"
        assert faith["recap"]["set_aside"][0]["reaction"] == "up"
        await c.delete(f"/api/reactions/{P.id}", params={"topic": FAITH.slug})
        agents = next(s for s in issue["sections"] if s["topic"]["slug"] == AGENTS.slug)
        assert len(agents["review"]["picks"]) == 3
        # A thumb on a pick, for the field it was picked for, comes back with the pick
        # wherever the review is shown (decision 17); a thumb for another topic does not.
        picked = agents["review"]["picks"][0]["paper_id"]
        r = await c.put(
            f"/api/reactions/{picked}",
            json={"topic": AGENTS.slug, "value": "down", "reason": "already_knew"},
        )
        assert r.status_code == 200 and r.json()["message"] == "Remembered for Agents"
        await c.put(f"/api/reactions/{picked}", json={"topic": FAITH.slug, "value": "up"})
        issue = (await c.get(f"/api/issues/{issue_id}")).json()
        agents = next(s for s in issue["sections"] if s["topic"]["slug"] == AGENTS.slug)
        first, *rest = agents["review"]["picks"]
        assert (first["reaction"], first["reaction_reason"]) == ("down", "already_knew")
        assert all(p["reaction"] is None for p in rest)
        listed = (await c.get(f"/api/topics/{AGENTS.slug}/reviews")).json()
        assert listed[0]["picks"][0]["reaction"] == "down"
        # A field topic's page lists its picks; its card says when it was last reviewed.
        table = (await c.get("/api/papers", params={"topic": AGENTS.slug})).json()
        assert table["total"] == 3
        card = next(t for t in (await c.get("/api/topics")).json() if t["slug"] == AGENTS.slug)
        assert card["last_review"] == monday.isoformat() and card["papers"] == 3
        assert card["new"] == 3  # picks since the page was last opened (never)
        listed = (await c.get(f"/api/topics/{AGENTS.slug}/reviews")).json()
        assert listed[0]["period"] == "week"
        # Recaps are not field reviews: a topic lists none, even after a change of kind.
        assert (await c.get(f"/api/topics/{FAITH.slug}/reviews")).json() == []
        # A topic that has since changed kind still reads as what was written then.
        await db.execute("UPDATE topics SET kind = 'watch' WHERE slug = %s", (AGENTS.slug,))
        issue = (await c.get(f"/api/issues/{issue_id}")).json()
        agents = next(s for s in issue["sections"] if s["topic"]["slug"] == AGENTS.slug)
        assert agents["review"] is not None and agents["recap"] is None
        await db.execute("UPDATE topics SET kind = 'pulse' WHERE slug = %s", (AGENTS.slug,))
    # Running the week again keeps what is written and asks nothing.
    before = len(models.calls)
    await job(db, models, lambda run: reviews.weekly(db, Config(), run, monday))
    assert models.calls[before:] == []


async def test_a_weekly_lede_that_failed_after_a_change_is_tried_again(db):
    models = Models()
    monday = Config().schedule.week_of(Config().schedule.today())
    await job(db, models, lambda run: reviews.weekly(db, Config(), run, monday))
    # A review fails, then is written on a run whose lede fails: the old lede is stale.
    await db.execute("UPDATE reviews SET state = 'failed' WHERE topic = %s", (AGENTS.slug,))
    await db.execute("DELETE FROM memberships WHERE paper_id = %s", (FIELD[0].id,))
    models.lede_down = True
    issue_id = await job(db, models, lambda run: reviews.weekly(db, Config(), run, monday))
    issue = await (await db.execute("SELECT * FROM issues WHERE id = %s", (issue_id,))).fetchone()
    assert issue["lede_stale"]
    # Nothing changes on the next run, but the stale lede is written again.
    models.lede_down = False
    before = len(models.calls)
    await job(db, models, lambda run: reviews.weekly(db, Config(), run, monday))
    assert models.calls[before:] == ["lede"]
    issue = await (await db.execute("SELECT * FROM issues WHERE id = %s", (issue_id,))).fetchone()
    assert not issue["lede_stale"]


def test_the_default_week_is_the_last_full_one():
    schedule = Config().schedule
    monday = schedule.week_of(date(2026, 10, 14))  # a Wednesday
    assert monday == date(2026, 10, 12) and monday.weekday() == 0
    assert schedule.window(monday, 7)[1] - schedule.window(monday, 7)[0] == timedelta(days=7)


async def test_a_topic_edit_records_no_set_aside(db):
    await verdicts.add(
        db, Verdict(paper_id=P.id, topic=FAITH.slug, topic_revision=1, grade=Grade.RELEVANT)
    )
    await word.settle(db, P.id)
    models = Models()
    await job(db, models, lambda run: digest.write_takes(db, Config(), run))
    await job(db, models, lambda run: digest.build_daily(db, Config(), run))
    await word.label(
        db,
        Label(
            paper_id=P.id, topic=FAITH.slug, value=LabelValue.RELEVANT, origin=LabelOrigin.READER
        ),
    )
    # The owner makes the Watch topic a Radar one: the paper leaves Watch, but not by their word.
    await topics_store.save(db, FAITH.model_copy(update={"kind": TopicKind.RADAR}))
    await word.settle_topic(db, FAITH.slug)
    assert await (await db.execute("SELECT * FROM set_asides")).fetchall() == []


async def test_the_status_line_counts_a_failed_review_until_it_is_written(db):
    models = Models()
    models.down = True
    await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run))
    models.down = False
    # A later run that fails nothing does not hide the earlier failure.
    await job(db, models, lambda run: digest.build_daily(db, Config(), run))
    dsn = f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"
    app = create_app(dsn, TOKEN, Config())
    headers = {"authorization": f"Bearer {TOKEN}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        assert (await c.get("/api/status")).json()["reviews_failed"] == 1
        await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run))
        assert (await c.get("/api/status")).json()["reviews_failed"] == 0


def test_without_a_configured_zone_the_servers_own_keeps_daylight_saving(monkeypatch):
    monkeypatch.setenv("TZ", "America/Denver")
    schedule = Config().schedule
    begin, end = schedule.window(date(2026, 10, 26), 7)  # the week clocks fall back
    assert (begin.utcoffset(), end.utcoffset()) == (timedelta(hours=-6), timedelta(hours=-7))
    assert end.astimezone(UTC) - begin.astimezone(UTC) == timedelta(days=7, hours=1)


async def test_taking_back_the_word_that_kept_a_paper_shown_sets_it_aside(db):
    # Graded adjacent, shown only because the owner said relevant, and announced so.
    await verdicts.add(
        db, Verdict(paper_id=P.id, topic=FAITH.slug, topic_revision=1, grade=Grade.ADJACENT)
    )
    await word.label(
        db,
        Label(
            paper_id=P.id, topic=FAITH.slug, value=LabelValue.RELEVANT, origin=LabelOrigin.READER
        ),
    )
    models = Models()
    await job(db, models, lambda run: digest.write_takes(db, Config(), run))
    await job(db, models, lambda run: digest.build_daily(db, Config(), run))
    await word.unlabel(db, P.id, FAITH.slug)
    rows = await (await db.execute("SELECT paper_id FROM set_asides")).fetchall()
    assert [r["paper_id"] for r in rows] == [P.id]


async def test_a_field_card_counts_a_paper_new_only_when_first_picked(db):
    models = Models()
    await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run))
    await db.execute("INSERT INTO topic_visits (topic) VALUES (%s)", (DAILY.slug,))
    # The weekly review picks the day's five again, and two more.
    monday = Config().schedule.week_of(Config().schedule.today())
    await job(db, models, lambda run: reviews.weekly(db, Config(), run, monday))
    cards = {c["slug"]: c for c in await reading_store.topic_cards(db)}
    assert cards[DAILY.slug]["papers"] == 7 and cards[DAILY.slug]["new"] == 2


async def test_a_review_that_failed_on_an_earlier_day_is_tried_again(db):
    models = Models()
    yesterday = Config().schedule.today() - timedelta(days=1)
    await db.execute(
        "UPDATE memberships SET created_at = %s WHERE topic = %s",
        (Config().schedule.window(yesterday)[0] + timedelta(hours=1), DAILY.slug),
    )
    models.down = True
    await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run, yesterday))
    models.down = False
    await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run))  # today's run
    row = await (
        await db.execute("SELECT state FROM reviews WHERE day = %s", (yesterday,))
    ).fetchone()
    assert row["state"] == "written"


async def test_the_default_weekly_run_retries_the_week_before(db):
    models = Models()
    last = Config().schedule.week_of(Config().schedule.today()) - timedelta(days=7)
    await db.execute(
        "INSERT INTO reviews (topic, kind, period, day, state) "
        "VALUES (%s, 'pulse', 'week', %s, 'failed')",
        (AGENTS.slug, last - timedelta(days=7)),
    )
    await job(db, models, lambda run: reviews.weekly(db, Config(), run))
    rows = await (
        await db.execute(
            "SELECT topic, state FROM reviews WHERE day = %s", (last - timedelta(days=7),)
        )
    ).fetchall()
    # Only the failed review is tried again; the week's other topics are left as they were.
    assert [(r["topic"], r["state"]) for r in rows] == [(AGENTS.slug, "quiet")]


async def test_an_empty_shortlist_still_reviews_a_sample_of_the_field(db, monkeypatch):
    monkeypatch.setattr(review_calls, "FULL_REVIEW_LIMIT", 3)
    monkeypatch.setattr(review_calls, "CHUNK", 3)
    models = Models()
    models.shortlist_empty = True
    await job(db, models, lambda run: reviews.daily_reviews(db, Config(), run))
    assert models.reviewed == [3]  # every third of the seven, not nothing


def test_a_posix_tz_rule_is_honoured_as_the_process_honours_it(monkeypatch):
    import time

    from litwatch.config import system_zone

    monkeypatch.setenv("TZ", "EST5EDT,M3.2.0,M11.1.0")
    time.tzset()
    try:
        offset = datetime.now(system_zone()).utcoffset()
        assert offset in (timedelta(hours=-5), timedelta(hours=-4))
    finally:
        monkeypatch.undo()
        time.tzset()


async def test_a_topics_reviews_run_newest_span_first_whatever_was_rewritten_last(db):
    monday = date(2026, 10, 5)
    for period, day, written in [
        ("week", monday, "2026-10-12 08:00"),  # the week of 5 October
        ("day", monday + timedelta(days=8), "2026-10-13 07:00"),  # Tuesday 13 October
        ("week", monday - timedelta(days=7), "2026-10-13 09:00"),  # an old week, retried last
    ]:
        await db.execute(
            "INSERT INTO reviews (topic, kind, period, day, state, written_at) "
            "VALUES (%s, 'radar', %s, %s, 'quiet', %s)",
            (DAILY.slug, period, day, written),
        )
    rows = await reviews_store.for_topic(db, DAILY.slug)
    assert [(r["period"], r["day"]) for r in rows] == [
        ("day", monday + timedelta(days=8)),
        ("week", monday),
        ("week", monday - timedelta(days=7)),
    ]


def test_a_failed_review_is_promised_a_retry_only_while_the_next_run_makes_it():
    schedule = Config().schedule
    today, monday = schedule.today(), schedule.week_of(schedule.today())
    assert reviews.will_retry(schedule, "day", today - timedelta(days=6))
    assert not reviews.will_retry(schedule, "day", today - timedelta(days=7))
    assert reviews.will_retry(schedule, "week", monday - timedelta(days=7))
    assert not reviews.will_retry(schedule, "week", monday - timedelta(days=14))
