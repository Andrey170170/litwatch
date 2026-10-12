"""What the writer and the daily issue owe a pair, and that each paper is announced once."""

import json
from datetime import date

import pytest

from litwatch.config import Config
from litwatch.core import (
    Grade,
    Reaction,
    ReactionReason,
    ReactionValue,
    TopicKind,
    Verdict,
)
from litwatch.feedback import word
from litwatch.jobs import digest
from litwatch.jobs.context import model_run
from litwatch.llm import Gateway, ProviderError
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts
from tests.conftest import FAITH, TOPICS, paper
from tests.fakes import ScriptedProvider

P = paper("2610.00001", title="Traces that hide steps")
Q = paper("2610.00002", title="A near neighbour")
DAY = date(2026, 10, 12)


class Writer:
    def __init__(self):
        self.down = False
        self.lede_down = False
        self.ledes = 0
        self.takes: list[str] = []

    def respond(self, role, request):
        if self.down or (self.lede_down and "lede" in request.system):
            raise ProviderError("InternalServerError")
        if "lede" in request.system:
            self.ledes += 1
            return json.dumps({"lede": "Lede."})
        self.takes.append(request.user.splitlines()[0])
        return json.dumps({"take": f"Take {len(self.takes)}."})

    def gateway(self, config, journal):
        providers = {n: ScriptedProvider(n, self.respond) for n in ("anthropic", "openrouter")}
        return Gateway(config.models, providers, journal, retry_delays=())


@pytest.fixture
async def db(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, [P, Q])
    return conn


async def grade(conn, p, g: str, revision=1) -> None:
    await verdicts.add(
        conn, Verdict(paper_id=p.id, topic=FAITH.slug, topic_revision=revision, grade=Grade(g))
    )
    await word.settle(conn, p.id)


async def night(conn, writer: Writer, day=DAY) -> int | None:
    async with model_run(conn, Config(), "nightly", gateway=writer.gateway) as run:
        await digest.write_takes(conn, Config(), run)
        return await digest.build_daily(conn, Config(), run, day)


async def rows(conn, sql):
    return await (await conn.execute(sql)).fetchall()


def unrelated(p) -> Reaction:
    return Reaction(
        paper_id=p.id,
        topic=FAITH.slug,
        value=ReactionValue.DOWN,
        reason=ReactionReason.UNRELATED,
    )


async def test_a_paper_crossing_the_line_gets_a_take_and_one_entry(db):
    writer = Writer()
    await grade(db, P, "relevant")
    issue_id = await night(db, writer)
    (entry,) = await rows(db, "SELECT * FROM issue_entries")
    assert entry["issue_id"] == issue_id and entry["take_id"] and entry["grade"] == "relevant"
    assert (await rows(db, "SELECT lede FROM issues"))[0]["lede"] == "Lede."
    assert await rows(db, "SELECT * FROM due") == []
    # The same day again amends nothing; the pair is never announced twice.
    assert await night(db, writer) == issue_id
    assert len(await rows(db, "SELECT * FROM issue_entries")) == 1


async def test_a_paper_rejected_before_its_issue_is_never_announced(db):
    writer = Writer()
    writer.down = True
    await grade(db, P, "relevant")
    await word.react(db, unrelated(P))
    writer.down = False
    assert await night(db, writer) is None
    assert await rows(db, "SELECT * FROM issue_entries") == []


async def test_set_aside_and_taken_back_is_not_announced_again(db):
    writer = Writer()
    await grade(db, P, "relevant")
    await night(db, writer)
    await word.react(db, unrelated(P))
    await word.unreact(db, P.id, FAITH.slug)
    # The take still fits the text, rubric and length, so nothing is owed.
    assert await rows(db, "SELECT * FROM due") == []
    await night(db, writer, date(2026, 10, 13))
    assert len(await rows(db, "SELECT * FROM issue_entries")) == 1 and len(writer.takes) == 1


async def test_a_move_to_must_read_rewrites_the_take_without_a_new_entry(db):
    writer = Writer()
    await grade(db, P, "relevant")
    await night(db, writer)
    await grade(db, P, "must_read")
    await night(db, writer, date(2026, 10, 13))
    takes = await rows(db, "SELECT grade FROM takes ORDER BY id")
    assert [t["grade"] for t in takes] == ["relevant", "must_read"]
    assert len(await rows(db, "SELECT * FROM issue_entries")) == 1


async def test_after_three_failed_runs_the_pair_enters_without_a_take(db):
    writer = Writer()
    writer.down = True
    await grade(db, P, "relevant")
    for _ in range(2):
        assert await night(db, writer) is None
    await night(db, writer)
    (entry,) = await rows(db, "SELECT * FROM issue_entries")
    assert entry["take_id"] is None
    # The writer keeps trying.
    (due,) = await rows(db, "SELECT * FROM due")
    assert due["for_take"] and not due["for_issue"] and due["failed_runs"] == 3


async def test_adjacent_arrivals_are_counted_but_make_no_issue_alone(db):
    writer = Writer()
    await grade(db, Q, "adjacent")
    assert await night(db, writer) is None
    await grade(db, P, "relevant")
    issue_id = await night(db, writer)
    (issue,) = await rows(db, f"SELECT folds FROM issues WHERE id = {issue_id}")
    assert issue["folds"] == {FAITH.slug: 1}


async def test_sync_owes_takes_but_announces_nothing(db):
    await verdicts.add(
        db, Verdict(paper_id=P.id, topic=FAITH.slug, topic_revision=1, grade=Grade.RELEVANT)
    )
    await word.sync(db)
    (due,) = await rows(db, "SELECT * FROM due")
    assert due["for_take"] and not due["for_issue"]
    assert await night(db, Writer()) is None


async def test_a_take_written_for_an_old_grade_leaves_the_new_one_owed(db):
    await grade(db, P, "relevant")
    (item,) = await digest._owed(db)
    await grade(db, P, "must_read")  # while the model was writing the short take
    item.text = "A short take."
    async with model_run(db, Config(), "nightly", gateway=Writer().gateway) as run:
        await digest._record(db, item, run)
    (due,) = await rows(db, "SELECT * FROM due")
    assert due["for_take"] is True


async def test_going_back_to_the_grade_a_take_fits_owes_no_rewrite(db):
    writer = Writer()
    await grade(db, P, "relevant")
    await night(db, writer)
    await grade(db, P, "must_read")
    await grade(db, P, "relevant")
    assert await rows(db, "SELECT * FROM due") == []


async def test_an_exhausted_rewrite_enters_without_the_outdated_take(db):
    writer = Writer()
    await grade(db, P, "relevant")
    # The short take exists before the paper is announced.
    async with model_run(db, Config(), "nightly", gateway=writer.gateway) as run:
        await digest.write_takes(db, Config(), run)
    await grade(db, P, "must_read")
    writer.down = True
    for _ in range(3):
        await night(db, writer)
    (entry,) = await rows(db, "SELECT * FROM issue_entries")
    assert (entry["grade"], entry["take_id"]) == ("must_read", None)


async def test_a_topic_no_longer_watch_gets_no_entry(db):
    writer = Writer()
    await grade(db, P, "relevant")
    async with model_run(db, Config(), "nightly", gateway=writer.gateway) as run:
        await digest.write_takes(db, Config(), run)
    await topics_store.save(db, FAITH.model_copy(update={"kind": TopicKind.RADAR}))
    assert await night(db, writer) is None
    assert await rows(db, "SELECT * FROM issue_entries") == []


async def test_a_failed_lede_is_written_by_a_later_build(db):
    writer = Writer()
    writer.lede_down = True
    await grade(db, P, "relevant")
    await night(db, writer)
    assert (await rows(db, "SELECT lede FROM issues"))[0]["lede"] is None
    writer.lede_down = False
    await night(db, writer, date(2026, 10, 13))
    assert (await rows(db, "SELECT lede FROM issues"))[0]["lede"] == "Lede."


async def test_a_rewritten_text_does_not_keep_its_old_take(db):
    writer = Writer()
    await grade(db, P, "relevant")
    async with model_run(db, Config(), "nightly", gateway=writer.gateway) as run:
        await digest.write_takes(db, Config(), run)
    await db.execute("UPDATE papers SET revision = 2 WHERE id = %s", (P.id,))
    await word.settle(db, P.id)
    for _ in range(3):  # the re-grade never comes; the writer waits for it
        await night(db, writer)
    (entry,) = await rows(db, "SELECT * FROM issue_entries")
    assert entry["take_id"] is None and len(writer.takes) == 1


async def test_a_take_the_owners_word_alone_asked_for_is_the_owners(db):
    await grade(db, P, "none")
    await word.react(
        db,
        Reaction(
            paper_id=P.id,
            topic=FAITH.slug,
            value=ReactionValue.UP,
            reason=ReactionReason.GREAT_FIND,
        ),
    )
    (item,) = await digest._owed(db)
    assert (item.source, item.opinions) == ("owner", [])


async def test_new_fold_counts_rewrite_the_lede(db):
    writer = Writer()
    await grade(db, P, "relevant")
    await night(db, writer)
    await grade(db, Q, "adjacent")
    await night(db, writer)
    (issue,) = await rows(db, "SELECT folds, lede_stale FROM issues")
    assert issue["folds"] == {FAITH.slug: 1} and issue["lede_stale"] is False
    assert writer.ledes == 2


async def test_a_rubric_edit_makes_takes_that_rest_on_the_owners_word_stale(db):
    from litwatch.cli import _save_all

    await word.react(
        db,
        Reaction(
            paper_id=P.id,
            topic=FAITH.slug,
            value=ReactionValue.UP,
            reason=ReactionReason.GREAT_FIND,
        ),
    )
    async with model_run(db, Config(), "nightly", gateway=Writer().gateway) as run:
        await digest.write_takes(db, Config(), run)
    assert (await rows(db, "SELECT for_take FROM due"))[0]["for_take"] is False
    await _save_all(db, [FAITH.model_copy(update={"rubric": "A new rubric."})])
    assert (await rows(db, "SELECT for_take FROM due"))[0]["for_take"] is True


async def test_a_late_result_that_no_longer_fits_leaves_the_take_owed(db):
    writer = Writer()
    await grade(db, P, "relevant")
    await night(db, writer)  # the short take, in an issue
    await grade(db, P, "must_read")
    (item,) = await digest._owed(db)  # the long take is being written ...
    await grade(db, P, "relevant")  # ... while the pair goes back: the short take fits again
    assert await rows(db, "SELECT * FROM due") == []
    item.text = "A long take."
    async with model_run(db, Config(), "nightly", gateway=writer.gateway) as run:
        await digest._record(db, item, run)
    (due,) = await rows(db, "SELECT * FROM due")
    assert due["for_take"] is True  # the newest take is the long one, which does not fit
