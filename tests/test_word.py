"""The owner's word: how verdicts, Weight, labels, thumbs and the Save toggle settle into effective
Fits and grades, rarity and the library, with their events."""

import pytest

from litwatch.core import (
    Analysis,
    Contribution,
    Depth,
    Facets,
    Fit,
    Grade,
    Hand,
    Label,
    LabelOrigin,
    LabelValue,
    Reaction,
    ReactionReason,
    ReactionValue,
    Verdict,
    Weight,
    in_library,
)
from litwatch.feedback import word
from litwatch.store import analyses as analyses_store
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store
from litwatch.store import verdicts
from tests.conftest import AGENTS, FAITH, INTERP, TREE, paper

P = paper("2610.00001")


def thumb(reason: str | None, value="down", topic=FAITH.slug) -> Reaction:
    return Reaction(
        paper_id=P.id,
        topic=topic,
        value=ReactionValue(value),
        reason=ReactionReason(reason) if reason else None,
    )


def said(value: str, topic=FAITH.slug, origin=LabelOrigin.READER) -> Label:
    return Label(paper_id=P.id, topic=topic, value=LabelValue(value), origin=origin)


def test_the_newest_hand_outranks_every_other_reason_to_be_saved():
    assert in_library(None, [Grade.RELEVANT, None]) is True
    assert in_library(None, [Grade.ADJACENT]) is False
    assert in_library(Hand.UNSAVE, [Grade.MUST_READ]) is False
    assert in_library(Hand.SAVE, []) is True


def test_a_reason_must_go_with_its_thumb():
    with pytest.raises(ValueError, match="does not go with"):
        thumb("great_find", "down")
    with pytest.raises(ValueError, match="does not go with"):
        thumb("overrated", "up")


async def classified(conn, revision: int = 1) -> None:
    await conn.execute(
        "INSERT INTO classifications (paper_id, paper_revision, taxonomy_revision, "
        "primary_class, model) VALUES (%s, %s, 'x', 'other', 'm')",
        (P.id, revision),
    )


@pytest.fixture
async def db(conn):
    """P classified: in the collection."""
    await taxonomy_store.save(conn, TREE)
    await papers_store.upsert(conn, [P])
    await classified(conn)
    return conn


async def graded(conn, fit: str, topic=FAITH.slug) -> None:
    await verdicts.add(
        conn,
        Verdict(
            paper_id=P.id,
            topic=topic,
            topic_version="v",
            fit=Fit(fit),
            reason="r",
            opinions={},
        ),
    )
    await word.settle(conn, P.id)


async def weighed(conn, weight: Weight) -> None:
    analysis = Analysis(
        gist="g",
        weight=weight,
        weight_reason="r",
        facets=Facets(
            contribution=Contribution.METHOD, releases_code=False, releases_weights=False
        ),
    )
    current = await papers_store.get(conn, P.id)
    await analyses_store.add(conn, current, "other", analysis, "m")
    await word.settle(conn, P.id)


async def outbox(conn) -> list[tuple[str, dict]]:
    rows = await (await conn.execute("SELECT type, payload FROM outbox ORDER BY id")).fetchall()
    return [(r["type"], r["payload"]) for r in rows if r["type"] != "paper.added"]


async def stored(conn, table: str) -> list[dict]:
    return await (await conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")).fetchall()


async def test_a_central_verdict_saves_the_paper_and_says_so(db):
    await graded(db, "central")
    assert await outbox(db) == [
        (
            "grade.changed",
            {
                "topic": FAITH.slug,
                "before": None,
                "after": "must_read",  # no Weight yet: solid
                "fit_before": None,
                "fit_after": "central",
            },
        ),
        ("paper.saved", {}),
    ]
    (row,) = await stored(db, "effective_grades")
    assert (row["fit"], row["grade"], row["rarity"]) == ("central", "must_read", "epic")
    # Settling again changes nothing and says nothing.
    await word.settle(db, P.id)
    assert len(await outbox(db)) == 2


async def test_weight_moves_the_grade_and_the_rarity(db):
    await graded(db, "related")
    assert (await word.settle(db, P.id)).saved  # related and solid: relevant
    await weighed(db, Weight.INCREMENTAL)
    (row,) = await stored(db, "effective_grades")
    assert (row["grade"], row["rarity"]) == ("adjacent", "uncommon")
    assert (await stored(db, "library"))[0]["saved"] is False
    _, (kind, payload), unsaved = (await outbox(db))[-3:]
    assert kind == "grade.changed" and (payload["before"], payload["after"]) == (
        "relevant",
        "adjacent",
    )
    assert unsaved == ("paper.unsaved", {})


async def test_area_and_field_topics_have_effective_grades_and_never_save(db):
    await graded(db, "central", topic=INTERP.slug)
    await graded(db, "central", topic=AGENTS.slug)
    rows = {r["topic"]: r for r in await stored(db, "effective_grades")}
    assert {t: r["grade"] for t, r in rows.items()} == {
        INTERP.slug: "must_read",
        AGENTS.slug: "must_read",
    }
    (rarity,) = await stored(db, "rarities")
    # Field's epic is lowered to uncommon; the Area's epic stands.
    assert (rarity["level"], rarity["best_topic"]) == ("epic", INTERP.slug)
    assert await stored(db, "library") == []


async def test_a_paper_with_a_weight_alone_has_its_floor(db):
    await weighed(db, Weight.LANDMARK)
    (rarity,) = await stored(db, "rarities")
    assert (rarity["level"], rarity["best_topic"]) == ("rare", None)


async def test_a_rewrite_drops_the_old_weight_until_the_new_text_is_analysed(db):
    await graded(db, "related")
    await weighed(db, Weight.INCREMENTAL)
    rewritten = P.model_copy(update={"abstract": "An entirely different abstract about robots."})
    await papers_store.upsert(db, [rewritten])
    assert (await papers_store.get(db, P.id)).revision == 2
    await word.settle(db, P.id)
    (row,) = await stored(db, "effective_grades")
    # The old verdict stands until the new text is graded, but its Weight went with the text.
    assert (row["fit"], row["grade"]) == ("related", "relevant")


async def test_only_a_paper_in_the_collection_has_a_rarity(db):
    await word.settle(db, P.id)
    (rarity,) = await stored(db, "rarities")  # classified, nothing else: solid's floor
    assert (rarity["level"], rarity["best_topic"]) == ("uncommon", None)
    unprocessed = paper("2610.00009")
    await papers_store.upsert(db, [unprocessed])
    await word.settle(db, unprocessed.id)
    assert [r["paper_id"] for r in await stored(db, "rarities")] == [P.id]


async def test_a_new_order_of_the_topics_breaks_a_tie_for_the_best_topic(db):
    await graded(db, "central")
    await graded(db, "central", topic=INTERP.slug)  # both epic in a mixed list
    assert (await stored(db, "rarities"))[0]["best_topic"] == FAITH.slug  # first in order
    await word.reorder(db, [INTERP.slug, FAITH.slug, AGENTS.slug])
    assert (await stored(db, "rarities"))[0]["best_topic"] == INTERP.slug
    assert (await word.save_taxonomy(db, TREE)).reordered  # the import's order, back again
    assert (await stored(db, "rarities"))[0]["best_topic"] == FAITH.slug


async def test_wrong_topic_takes_the_paper_out_and_clearing_the_thumb_brings_it_back(db):
    await graded(db, "central")
    _, settled = await word.react(db, thumb("wrong_topic"))
    assert settled.grades == {FAITH.slug: (Grade.MUST_READ, Grade.NONE)}
    assert (settled.was_saved, settled.saved) == (True, False)
    settled = await word.unreact(db, P.id, FAITH.slug)
    assert settled.grades == {FAITH.slug: (Grade.NONE, Grade.MUST_READ)}
    assert settled.saved is True


async def test_a_great_find_lifts_an_adjacent_verdict_to_related(db):
    await graded(db, "adjacent")
    _, settled = await word.react(db, thumb("great_find", "up"))
    assert settled.grades == {FAITH.slug: (Grade.ADJACENT, Grade.RELEVANT)} and settled.saved


async def test_reactions_that_carry_no_word_change_nothing(db):
    await graded(db, "related")
    await graded(db, "related", topic=INTERP.slug)
    for reaction in (
        thumb("not_interesting"),
        thumb("wrong_topic", topic=INTERP.slug),
        thumb(None),
    ):
        _, settled = await word.react(db, reaction)
        assert settled.grades == {} and settled.saved is True
    _, settled = await word.react(db, thumb("wrong_topic", topic=None))
    assert settled.grades == {}


async def test_a_label_is_the_owners_word_on_any_followed_topic(db):
    _, settled = await word.label(db, said("related", topic=INTERP.slug))
    assert settled.grades == {INTERP.slug: (None, Grade.RELEVANT)}
    assert settled.saved is False  # an Area paper is saved by hand


async def test_the_owners_related_never_falls_below_relevant(db):
    await weighed(db, Weight.INCREMENTAL)
    await graded(db, "none")
    _, settled = await word.label(db, said("related"))
    assert settled.grades == {FAITH.slug: (Grade.NONE, Grade.RELEVANT)} and settled.saved


async def test_a_label_after_a_thumb_decides_and_keeps_its_origin(db):
    await graded(db, "related")
    await word.react(db, thumb("wrong_topic"))
    first, settled = await word.label(db, said("central", origin=LabelOrigin.SAMPLE))
    assert settled.grades == {FAITH.slug: (Grade.NONE, Grade.MUST_READ)} and settled.saved
    changed, _ = await word.label(db, first.model_copy(update={"origin": LabelOrigin.READER}))
    assert changed.origin == LabelOrigin.SAMPLE


async def test_unsave_holds_against_a_later_regrade_until_saved_again(db):
    await graded(db, "related")
    settled = await word.toggle(db, P.id, Hand.UNSAVE)
    assert (settled.was_saved, settled.saved) == (True, False)
    await graded(db, "central")
    assert (await word.settle(db, P.id)).saved is False
    assert (await word.toggle(db, P.id, Hand.SAVE)).saved is True
    types = [t for t, _ in await outbox(db)]
    assert types.count("paper.saved") == 2 and types.count("paper.unsaved") == 1


async def test_a_paper_saved_by_hand_needs_no_topic(db):
    settled = await word.toggle(db, P.id, Hand.SAVE)
    assert settled.saved and settled.grades == {}


async def test_sync_settles_what_was_written_around_it(db):
    await verdicts.add(
        db,
        Verdict(
            paper_id=P.id, topic=FAITH.slug, topic_version="v", fit=Fit.CENTRAL, reason="r",
            opinions={},
        ),
    )  # fmt: skip
    assert await word.sync(db) == {"papers": 1, "grades_changed": 1, "saved": 1, "unsaved": 0}
    assert await word.sync(db) == {"papers": 1, "grades_changed": 0, "saved": 0, "unsaved": 0}


async def test_the_later_word_wins_inside_one_transaction(db):
    async with db.transaction():
        await word.react(db, thumb("wrong_topic"))
        _, settled = await word.label(db, said("central"))
    assert settled.saved is True
    async with db.transaction():
        await word.label(db, said("central"))
        _, settled = await word.react(db, thumb("wrong_topic"))
    assert settled.saved is False


@pytest.mark.parametrize("unfollow", [False, True])
async def test_a_topic_no_longer_followed_at_focus_leaves_the_library(db, unfollow):
    await graded(db, "related")
    area = FAITH.model_copy(update={"depth": Depth.AREA, "rubric": None})
    tree = TREE.model_copy(update={"topics": [INTERP]}) if unfollow else TREE.following(area)
    assert FAITH.slug in (await word.save_taxonomy(db, tree)).changed_topics
    assert (await (await db.execute("SELECT saved FROM library")).fetchone())["saved"] is False
    rows = await stored(db, "effective_grades")
    # Followed at Area, it keeps its grade there; unfollowed, the grade goes with its event.
    assert [r["grade"] for r in rows] == ([] if unfollow else ["relevant"])
    if unfollow:
        _, after = (await outbox(db))[-2]  # the grade's event, then the unsave
        assert (after["topic"], after["before"], after["after"]) == (FAITH.slug, "relevant", None)
