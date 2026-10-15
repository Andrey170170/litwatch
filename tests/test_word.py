"""The owner's word: how verdicts, labels, thumbs and the Save toggle settle into effective
grades and the library, with their events."""

from datetime import UTC, datetime, timedelta

import pytest

from litwatch.core import (
    Depth,
    Grade,
    Hand,
    Label,
    LabelOrigin,
    LabelValue,
    Reaction,
    ReactionReason,
    ReactionValue,
    Verdict,
    in_library,
    owners_word,
)
from litwatch.feedback import word
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts
from tests.conftest import FAITH, INTERP, TREE, paper

T0 = datetime(2026, 10, 1, tzinfo=UTC)
P = paper("2610.00001")


def label(value: str, at=T0, provisional=False, grade=None) -> Label:
    return Label(
        paper_id=P.id,
        topic=FAITH.slug,
        value=LabelValue(value),
        grade=grade,
        origin=LabelOrigin.READER,
        provisional=provisional,
        labelled_at=at,
    )


def thumb(reason: str | None, value="down", topic=FAITH.slug, at=T0) -> Reaction:
    return Reaction(
        paper_id=P.id,
        topic=topic,
        value=ReactionValue(value),
        reason=ReactionReason(reason) if reason else None,
        reacted_at=at,
    )


def test_the_newer_of_label_and_word_carrying_thumb_is_the_owners_word():
    later = T0 + timedelta(hours=1)
    assert owners_word(label("relevant"), thumb("unrelated", at=later)).value == "not_relevant"
    assert owners_word(label("relevant", at=later), thumb("unrelated")).value == "relevant"
    # A bulk-accepted label loses to any thumb that speaks, whatever the times.
    assert owners_word(label("relevant", at=later, provisional=True), thumb("unrelated")).value == (
        "not_relevant"
    )
    # Reasons that do not speak to the topic, and unsure labels, are no word.
    assert owners_word(None, thumb("not_for_me")) is None
    assert owners_word(label("unsure"), thumb("great_find", "up")).value == "relevant"
    assert owners_word(label("unsure"), None) is None


def test_a_reason_must_go_with_its_thumb():
    with pytest.raises(ValueError, match="does not go with"):
        thumb("great_find", "down")
    with pytest.raises(ValueError, match="does not go with"):
        thumb("overrated", "up")


def test_the_newest_hand_outranks_every_other_reason_to_be_saved():
    assert in_library(None, [Grade.RELEVANT, None]) is True
    assert in_library(None, [Grade.ADJACENT]) is False
    assert in_library(Hand.UNSAVE, [Grade.MUST_READ]) is False
    assert in_library(Hand.SAVE, []) is True


@pytest.fixture
async def db(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P])
    return conn


async def grade(conn, g: str) -> None:
    await verdicts.add(
        conn, Verdict(paper_id=P.id, topic=FAITH.slug, topic_revision=1, grade=Grade(g))
    )
    await word.settle(conn, P.id)


async def outbox(conn) -> list[tuple[str, dict]]:
    rows = await (await conn.execute("SELECT type, payload FROM outbox ORDER BY id")).fetchall()
    return [(r["type"], r["payload"]) for r in rows if r["type"] != "paper.added"]


async def test_a_relevant_verdict_saves_the_paper_and_says_so(db):
    await grade(db, "relevant")
    assert await outbox(db) == [
        ("grade.changed", {"topic": FAITH.slug, "before": None, "after": "relevant"}),
        ("paper.saved", {}),
    ]
    # Settling again changes nothing and says nothing.
    await word.settle(db, P.id)
    assert len(await outbox(db)) == 2


async def test_unrelated_takes_the_paper_out_and_clearing_the_thumb_brings_it_back(db):
    await grade(db, "must_read")
    _, settled = await word.react(db, thumb("unrelated"))
    assert settled.grades == {FAITH.slug: (Grade.MUST_READ, Grade.NONE)}
    assert (settled.was_saved, settled.saved) == (True, False)
    settled = await word.unreact(db, P.id, FAITH.slug)
    assert settled.grades == {FAITH.slug: (Grade.NONE, Grade.MUST_READ)}
    assert settled.saved is True


async def test_reactions_that_carry_no_word_change_nothing(db):
    await grade(db, "relevant")
    for reaction in (thumb("not_for_me"), thumb("unrelated", topic=INTERP.slug), thumb(None)):
        _, settled = await word.react(db, reaction)
        assert settled.grades == {} and settled.saved is True
    _, settled = await word.react(db, thumb("unrelated", topic=None))
    assert settled.grades == {}


async def test_a_label_after_a_thumb_decides_and_keeps_its_origin(db):
    await grade(db, "relevant")
    await word.react(db, thumb("unrelated"))
    stored, settled = await word.label(
        db,
        Label(
            paper_id=P.id, topic=FAITH.slug, value=LabelValue.RELEVANT, origin=LabelOrigin.SAMPLE
        ),
    )
    assert settled.grades == {FAITH.slug: (Grade.NONE, Grade.RELEVANT)} and settled.saved
    changed, _ = await word.label(db, stored.model_copy(update={"origin": LabelOrigin.READER}))
    assert changed.origin == LabelOrigin.SAMPLE


async def test_unsave_holds_against_a_later_regrade_until_saved_again(db):
    await grade(db, "relevant")
    settled = await word.toggle(db, P.id, Hand.UNSAVE)
    assert (settled.was_saved, settled.saved) == (True, False)
    await grade(db, "must_read")
    assert (await word.settle(db, P.id)).saved is False
    assert (await word.toggle(db, P.id, Hand.SAVE)).saved is True
    types = [t for t, _ in await outbox(db)]
    assert types.count("paper.saved") == 2 and types.count("paper.unsaved") == 1


async def test_a_paper_saved_by_hand_needs_no_topic(db):
    settled = await word.toggle(db, P.id, Hand.SAVE)
    assert settled.saved and settled.grades == {}


async def test_sync_settles_what_was_written_around_it(db):
    await verdicts.add(
        db, Verdict(paper_id=P.id, topic=FAITH.slug, topic_revision=1, grade=Grade.RELEVANT)
    )
    assert await word.sync(db) == {"papers": 1, "grades_changed": 1, "saved": 1, "unsaved": 0}
    assert await word.sync(db) == {"papers": 1, "grades_changed": 0, "saved": 0, "unsaved": 0}


async def test_the_later_word_wins_inside_one_transaction(db):
    relevant = Label(
        paper_id=P.id, topic=FAITH.slug, value=LabelValue.RELEVANT, origin=LabelOrigin.READER
    )
    async with db.transaction():
        await word.react(db, thumb("unrelated"))
        _, settled = await word.label(db, relevant)
    assert settled.saved is True
    async with db.transaction():
        await word.label(db, relevant)
        _, settled = await word.react(db, thumb("unrelated"))
    assert settled.saved is False


@pytest.mark.parametrize("unfollow", [False, True])
async def test_a_topic_no_longer_followed_at_focus_takes_its_grade_with_it(db, unfollow):
    await grade(db, "relevant")
    area = FAITH.model_copy(update={"depth": Depth.AREA, "rubric": None})
    tree = TREE.model_copy(update={"topics": [INTERP]}) if unfollow else TREE.following(area)
    assert FAITH.slug in (await word.save_taxonomy(db, tree)).changed_topics
    assert (await (await db.execute("SELECT saved FROM library")).fetchone())["saved"] is False
    rows = await (await db.execute("SELECT * FROM effective_grades")).fetchall()
    assert rows == []
    _, after = (await outbox(db))[-2]  # the grade's event, then the unsave
    assert after == {"topic": FAITH.slug, "before": "relevant", "after": None}
