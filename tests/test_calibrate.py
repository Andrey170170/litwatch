"""Calibrate: the draw, answers and what they become, precision, and the no-topic list."""

import pytest

from litwatch.core import (
    Grade,
    Label,
    LabelOrigin,
    LabelValue,
    Opinion,
    Reaction,
    ReactionReason,
    ReactionValue,
    Verdict,
)
from litwatch.feedback import calibrate, word
from litwatch.feedback.calibrate import Answer, Fit
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store
from litwatch.store import verdicts
from tests.conftest import FAITH, TOPICS, paper

ABOVE = [paper(f"2610.0010{i}") for i in range(3)]
BELOW = [paper(f"2610.0020{i}") for i in range(3)]


@pytest.fixture
async def db(conn):
    for topic in TOPICS:
        await topics_store.save(conn, topic)
    await papers_store.upsert(conn, ABOVE + BELOW)
    for p, g in [(p, "relevant") for p in ABOVE] + [(p, "none") for p in BELOW]:
        await verdicts.add(
            conn, Verdict(paper_id=p.id, topic=FAITH.slug, topic_revision=1, grade=Grade(g))
        )
        await word.settle(conn, p.id)
    return conn


async def items(conn, session_id):
    return (await calibrate.session(conn, session_id))["items"]


async def test_a_draw_is_even_across_groups_seeded_and_skips_labelled_papers(db):
    await word.label(
        db,
        Label(
            paper_id=ABOVE[0].id,
            topic=FAITH.slug,
            value=LabelValue.RELEVANT,
            origin=LabelOrigin.READER,
        ),
    )
    first = await items(db, await calibrate.draw(db, seed=7, size=4))
    assert sorted(i["why"] for i in first) == ["above", "above", "below", "below"]
    assert ABOVE[0].id not in {i["paper_id"] for i in first}
    again = await items(db, await calibrate.draw(db, seed=7, size=4))
    assert [i["paper_id"] for i in again] == [i["paper_id"] for i in first]


async def test_answers_become_labels_appraisals_and_take_ratings(db):
    session_id = await calibrate.draw(db, seed=1, size=2)
    (item,) = [i for i in await items(db, session_id) if i["why"] == "above"]
    with pytest.raises(ValueError, match="required"):
        await calibrate.answer(db, session_id, item["position"], Answer(fit={}))
    await calibrate.answer(
        db,
        session_id,
        item["position"],
        Answer(
            fit={FAITH.slug: Fit(value=LabelValue.NOT_RELEVANT)},
            value="matters",
            note="about steering, not traces",
        ),
    )
    label = await (
        await db.execute("SELECT * FROM labels WHERE paper_id = %s", (item["paper_id"],))
    ).fetchone()
    assert (label["value"], label["origin"]) == ("not_relevant", "sample")
    # It matters but fits no Watch topic: a missing topic or a narrow rubric.
    (listed,) = await calibrate.matters_no_topic(db)
    assert (listed["paper_id"], listed["note"]) == (item["paper_id"], "about steering, not traces")


async def test_precision_judges_the_drawn_verdict_and_ignores_the_unanswered(db):
    session_id = await calibrate.draw(db, seed=3, size=6)
    above = [i for i in await items(db, session_id) if i["why"] == "above"]
    below = [i for i in await items(db, session_id) if i["why"] == "below"]
    await calibrate.answer(
        db, session_id, above[0]["position"], Answer(fit={FAITH.slug: Fit(value="relevant")})
    )
    await calibrate.answer(
        db, session_id, above[1]["position"], Answer(fit={FAITH.slug: Fit(value="not_relevant")})
    )
    await calibrate.answer(
        db, session_id, below[0]["position"], Answer(fit={FAITH.slug: Fit(value="unsure")})
    )
    (row,) = await calibrate.precision(db)
    assert (row["precision"], row["precision_n"]) == (0.5, 2)
    assert (row["dropped"], row["dropped_n"]) == (None, 0)


async def test_more_takes_in_straddling_splits_and_thumbed_papers_once(db):
    extra = paper("2610.00300")
    await papers_store.upsert(db, [extra])
    await verdicts.add(
        db,
        Verdict(
            paper_id=extra.id,
            topic=FAITH.slug,
            topic_revision=1,
            grade=Grade.RELEVANT,
            split=True,
            judge=Opinion(grade=Grade.RELEVANT, reason="r"),
            check=Opinion(grade=Grade.NONE, reason="r"),
        ),
    )
    await word.react(
        db,
        Reaction(
            paper_id=BELOW[0].id,
            topic=FAITH.slug,
            value=ReactionValue.UP,
            reason=ReactionReason.GREAT_FIND,
        ),
    )
    session_id = await calibrate.draw(db, seed=5, size=0)
    assert await calibrate.more(db, session_id) == 2
    assert {(i["paper_id"], i["why"]) for i in await items(db, session_id)} >= {
        (extra.id, "split"),
        (BELOW[0].id, "reaction"),
    }
    assert await calibrate.more(db, session_id) == 0
