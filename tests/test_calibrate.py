"""Calibrate: the draw, answers and what they become, precision, and the no-topic list."""

import pytest

from litwatch.core import (
    Cause,
    Depth,
    Fit,
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
from litwatch.feedback.calibrate import Answer
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts
from tests.conftest import AGENTS, FAITH, TREE, paper

ABOVE = [paper(f"2610.0010{i}") for i in range(3)]
BELOW = [paper(f"2610.0020{i}") for i in range(3)]


@pytest.fixture
async def db(conn):
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, ABOVE + BELOW)
    for p, fit in [(p, "related") for p in ABOVE] + [(p, "none") for p in BELOW]:
        await verdicts.add(conn, verdict(p.id, Fit(fit)))
        await word.settle(conn, p.id, cause=Cause.VERDICT)
    return conn


def verdict(paper_id: str, fit: Fit, **opinions: Fit) -> Verdict:
    return Verdict(
        paper_id=paper_id,
        topic=FAITH.slug,
        topic_version="v",
        fit=fit,
        reason="r",
        opinions={role: Opinion(model="m", fit=f, reason="r") for role, f in opinions.items()},
    )


async def items(conn, session_id):
    return (await calibrate.session(conn, session_id))["items"]


async def test_a_draw_is_even_across_groups_seeded_and_skips_labelled_papers(db):
    await word.label(
        db,
        Label(
            paper_id=ABOVE[0].id,
            topic=FAITH.slug,
            value=LabelValue.RELATED,
            origin=LabelOrigin.READER,
        ),
    )
    first = await items(db, await calibrate.draw(db, seed=7, size=4))
    assert sorted(i["why"] for i in first) == ["above", "above", "below", "below"]
    assert ABOVE[0].id not in {i["paper_id"] for i in first}
    again = await items(db, await calibrate.draw(db, seed=7, size=4))
    assert [i["paper_id"] for i in again] == [i["paper_id"] for i in first]


async def test_a_draw_skips_a_verdict_on_text_since_rewritten(db):
    rewritten = ABOVE[0].model_copy(update={"abstract": "An entirely different abstract."})
    await papers_store.upsert(db, [rewritten])
    drawn = await items(db, await calibrate.draw(db, seed=7, size=6))
    assert ABOVE[0].id not in {i["paper_id"] for i in drawn}
    assert sorted(i["why"] for i in drawn) == ["above", "above", "below", "below", "below"]


async def test_answers_become_labels_and_appraisals(db):
    session_id = await calibrate.draw(db, seed=1, size=2)
    (item,) = [i for i in await items(db, session_id) if i["why"] == "above"]
    assert item["verdict"] == "related"  # the pipeline's side, for the review
    with pytest.raises(ValueError, match="required"):
        await calibrate.answer(db, session_id, item["position"], Answer(fit={}))
    await calibrate.answer(
        db,
        session_id,
        item["position"],
        Answer(
            fit={FAITH.slug: LabelValue.NONE},
            value="matters",
            note="about steering, not traces",
        ),
    )
    label = await (
        await db.execute("SELECT * FROM labels WHERE paper_id = %s", (item["paper_id"],))
    ).fetchone()
    assert (label["value"], label["origin"]) == ("none", "sample")
    # The answer is recorded on the item, for the review, whatever the label says later.
    (item,) = [i for i in await items(db, session_id) if i["why"] == "above"]
    assert item["answer"] == "none" and item["answered_at"] is not None
    # It matters but fits no Focus topic: a missing topic or a narrow rubric.
    (listed,) = await calibrate.matters_no_topic(db)
    assert (listed["paper_id"], listed["note"]) == (item["paper_id"], "about steering, not traces")


async def test_a_fit_moved_between_topics_keeps_the_paper_in_the_library_throughout(db):
    """An item's labels are written together and settled once: moving its Fit from one Focus
    topic to another emits no unsave and save between them."""
    second = AGENTS.model_copy(update={"depth": Depth.FOCUS, "rubric": "Tool use, end to end."})
    await word.save_taxonomy(db, TREE.following(second))
    session_id = await calibrate.draw(db, seed=1, size=4)
    # Drawn above the line on its Focus topic: graded relevant, so in the library.
    (item,) = [
        i for i in await items(db, session_id) if i["topic"] == FAITH.slug and i["why"] == "above"
    ]
    before = await library_events(db, item["paper_id"])
    moved = {FAITH.slug: LabelValue.NONE, AGENTS.slug: LabelValue.RELATED}
    await calibrate.answer(db, session_id, item["position"], Answer(fit=moved))
    assert await library_events(db, item["paper_id"]) == before


async def library_events(conn, paper_id: str) -> list[str]:
    rows = await (
        await conn.execute(
            "SELECT type FROM outbox WHERE paper_id = %s AND type IN ('paper.saved', "
            "'paper.unsaved') ORDER BY id",
            (paper_id,),
        )
    ).fetchall()
    return [r["type"] for r in rows]


async def test_precision_judges_the_drawn_verdict_and_ignores_the_unanswered(db):
    session_id = await calibrate.draw(db, seed=3, size=6)
    above = [i for i in await items(db, session_id) if i["why"] == "above"]
    below = [i for i in await items(db, session_id) if i["why"] == "below"]
    await calibrate.answer(
        db,
        session_id,
        above[0]["position"],
        Answer(fit={FAITH.slug: LabelValue.RELATED}),
    )
    await calibrate.answer(
        db, session_id, above[1]["position"], Answer(fit={FAITH.slug: LabelValue.NONE})
    )
    await calibrate.answer(
        db, session_id, below[0]["position"], Answer(fit={FAITH.slug: LabelValue.UNSURE})
    )
    (row,) = await calibrate.precision(db)
    assert (row["precision"], row["precision_n"]) == (0.5, 2)
    assert (row["dropped"], row["dropped_n"]) == (None, 0)


async def test_more_takes_in_straddling_splits_and_thumbed_papers_once(db):
    extra = paper("2610.00300")
    await papers_store.upsert(db, [extra])
    await verdicts.add(
        db,
        verdict(extra.id, Fit.RELATED, judge=Fit.RELATED, check=Fit.NONE).model_copy(
            update={"split": True}
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


async def test_an_answer_counts_even_when_the_paper_had_a_label_since_the_draw(db):
    session_id = await calibrate.draw(db, seed=3, size=2)
    (item,) = [i for i in await items(db, session_id) if i["why"] == "above"]
    await word.label(
        db,
        Label(
            paper_id=item["paper_id"],
            topic=FAITH.slug,
            value=LabelValue.RELATED,
            origin=LabelOrigin.READER,
        ),
    )
    await calibrate.answer(
        db,
        session_id,
        item["position"],
        Answer(fit={FAITH.slug: LabelValue.RELATED}),
    )
    (row,) = await calibrate.precision(db)
    assert (row["precision"], row["precision_n"]) == (1.0, 1)


async def test_more_takes_in_an_unmarked_miss_for_a_focus_topic_only(db):
    missed, elsewhere = paper("2610.00400"), paper("2610.00401")
    await papers_store.upsert(db, [missed, elsewhere])
    await db.execute(
        "INSERT INTO misses (paper_id, topic) VALUES (%s, %s), (%s, 'interpretability')",
        (missed.id, FAITH.slug, elsewhere.id),
    )
    session_id = await calibrate.draw(db, seed=5, size=0)
    await calibrate.more(db, session_id)
    taken = {(i["paper_id"], i["why"]) for i in await items(db, session_id)}
    assert (missed.id, "miss") in taken
    assert (elsewhere.id, "miss") not in taken  # no label can be given for an Area topic


async def test_a_miss_keeps_the_verdict_standing_when_it_joins(db):
    """A past review shows what the pipeline said when the paper joined the session, not a
    later regrade."""
    missed = ABOVE[0]
    await db.execute(
        "INSERT INTO misses (paper_id, topic) VALUES (%s, %s)", (missed.id, FAITH.slug)
    )
    session_id = await calibrate.draw(db, seed=5, size=0)
    await calibrate.more(db, session_id)
    await verdicts.add(db, verdict(missed.id, Fit.CENTRAL))  # regraded after the session
    (item,) = [i for i in await items(db, session_id) if i["why"] == "miss"]
    assert item["verdict"] == "related"


async def test_an_answer_does_not_renew_a_label_it_leaves_as_it_was(db):
    """Editing only the appraisal sends the drawn Fit again; the label keeps its time, so a
    reaction given since still outranks it."""
    session_id = await calibrate.draw(db, seed=1, size=2)
    (item,) = [i for i in await items(db, session_id) if i["why"] == "above"]
    fit = {FAITH.slug: LabelValue.NONE}
    await calibrate.answer(db, session_id, item["position"], Answer(fit=fit))
    at = "SELECT labelled_at FROM labels WHERE paper_id = %s AND topic = %s"
    before = await (await db.execute(at, (item["paper_id"], FAITH.slug))).fetchone()
    await calibrate.answer(db, session_id, item["position"], Answer(fit=fit, value="matters"))
    after = await (await db.execute(at, (item["paper_id"], FAITH.slug))).fetchone()
    assert after["labelled_at"] == before["labelled_at"]


async def test_an_appraisal_edit_after_the_first_answer_writes_no_label(db):
    """The drawn topic's Fit is required on the first answer only. An edit of the appraisal
    alone leaves the Fit out, so a label the owner has since taken back is not recreated from
    the recorded answer; the answer stands as recorded."""
    session_id = await calibrate.draw(db, seed=1, size=2)
    (item,) = [i for i in await items(db, session_id) if i["why"] == "above"]
    with pytest.raises(ValueError, match="required"):
        await calibrate.answer(db, session_id, item["position"], Answer(fit={}, value="fine"))
    fit = {FAITH.slug: LabelValue.NONE}
    await calibrate.answer(db, session_id, item["position"], Answer(fit=fit))
    await word.unlabel(db, item["paper_id"], FAITH.slug)
    await calibrate.answer(db, session_id, item["position"], Answer(fit={}, value="matters"))
    labels = await (
        await db.execute("SELECT * FROM labels WHERE paper_id = %s", (item["paper_id"],))
    ).fetchall()
    assert labels == []
    (item,) = [i for i in await items(db, session_id) if i["why"] == "above"]
    assert (item["answer"], item["fit"], item["appraisal"]["value"]) == ("none", {}, "matters")


async def test_fit_names_only_focus_topics(db):
    session_id = await calibrate.draw(db, seed=1, size=2)
    (item,) = [i for i in await items(db, session_id) if i["why"] == "above"]
    fit = {FAITH.slug: LabelValue.NONE, "interpretability": LabelValue.NONE}
    with pytest.raises(ValueError, match="not Focus topics: interpretability"):
        await calibrate.answer(db, session_id, item["position"], Answer(fit=fit))


async def test_an_appraisal_can_be_cleared(db):
    session_id = await calibrate.draw(db, seed=1, size=2)
    (item,) = [i for i in await items(db, session_id) if i["why"] == "above"]
    fit = {FAITH.slug: LabelValue.NONE}
    await calibrate.answer(db, session_id, item["position"], Answer(fit=fit, value="matters"))
    await calibrate.answer(db, session_id, item["position"], Answer(fit=fit, value="matters"))
    assert len(await calibrate.matters_no_topic(db)) == 1
    await calibrate.answer(db, session_id, item["position"], Answer(fit=fit))
    assert await calibrate.matters_no_topic(db) == []
    rows = await (await db.execute("SELECT count(*) AS n FROM appraisals")).fetchone()
    assert rows["n"] == 2  # matters once, then cleared


async def test_the_sessions_list_counts_answers_that_differ_from_the_verdict(db):
    """The past-session rows say how many answers differ from the pipeline's Fit: another Fit
    than the verdict's, as the review marks them. Unsure never differs."""
    import httpx

    from litwatch.api import create_app
    from litwatch.config import Config

    session_id = await calibrate.draw(db, seed=3, size=4)
    above = [i for i in await items(db, session_id) if i["why"] == "above"]  # verdict related
    below = [i for i in await items(db, session_id) if i["why"] == "below"]  # verdict none
    for item, fit in (
        (above[0], LabelValue.NONE),  # differs
        (above[1], LabelValue.CENTRAL),  # differs: another Fit, though on the same side
        (below[0], LabelValue.RELATED),  # differs
        (below[1], LabelValue.UNSURE),  # answered, never differs
    ):
        await calibrate.answer(db, session_id, item["position"], Answer(fit={FAITH.slug: fit}))
    dsn = f"{db.test_dsn}?options=-csearch_path%3D{db.test_schema}"
    transport = httpx.ASGITransport(app=create_app(dsn, "t", Config()))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"authorization": "Bearer t"}
    ) as c:
        (row,) = (await c.get("/api/settings/calibrate")).json()
    assert (row["papers"], row["answered"], row["differ"]) == (4, 4, 3)
