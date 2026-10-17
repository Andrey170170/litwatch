"""The pure rules of grading (pipeline.md, sections 4, 5 and 8; feedback.md): which topics grade a
paper, the grade table and the owner's floor, the owner's word as a Fit, rarity, and budgets."""

from datetime import UTC, datetime, timedelta

import pytest

from litwatch import seeds
from litwatch.core import (
    Depth,
    Fit,
    Grade,
    Label,
    LabelOrigin,
    LabelValue,
    Rarity,
    Reaction,
    ReactionReason,
    ReactionValue,
    Status,
    Weight,
    effective,
    grade,
    grading_topics,
    mixed_rarity,
    owners_word,
)
from litwatch.core.budget import projected, spend
from litwatch.core.classification import Membership, Source

TREE = seeds.taxonomy()  # Interpretability an Area, CoT faithfulness a Focus topic under it
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def members(*slugs: str, source: Source = Source.CLASSIFIER) -> list[Membership]:
    return [Membership(slug=s, source=source) for s in slugs]


def test_which_topics_grade_a_paper_the_examples_of_section_4():
    # direct member of Sparse autoencoders only: graded for Interpretability;
    assert grading_topics(TREE, members("sparse-autoencoders")) == {"interpretability"}
    # direct member of CoT faithfulness only: graded for CoT faithfulness, not Interpretability;
    assert grading_topics(TREE, members("cot-faithfulness")) == {"cot-faithfulness"}
    # direct member of CoT faithfulness and of Sparse autoencoders: graded for both;
    both = members("cot-faithfulness", "sparse-autoencoders")
    assert grading_topics(TREE, both) == {"cot-faithfulness", "interpretability"}
    # direct member of CoT faithfulness and of Interpretability itself: graded for both.
    both = members("cot-faithfulness", "interpretability")
    assert grading_topics(TREE, both) == {"cot-faithfulness", "interpretability"}
    # No direct membership under any followed class: not graded.
    assert grading_topics(TREE, members("robotics")) == set()


def test_the_band_and_the_keyword_check_count_for_their_focus_topic_only():
    band = members("cot-faithfulness", source=Source.UNCERTAIN)
    assert grading_topics(TREE, band) == {"cot-faithfulness"}
    # A band row of a class that is not (or no longer) a Focus topic leads nowhere: it never
    # falls through to the Area above.
    assert grading_topics(TREE, members("sparse-autoencoders", source=Source.KEYWORD)) == set()
    unfollowed = TREE.model_copy(
        update={"topics": [t for t in TREE.topics if t.slug != "cot-faithfulness"]}
    )
    assert grading_topics(unfollowed, band) == set()
    assert grading_topics(unfollowed, members("cot-faithfulness")) == {"interpretability"}


def test_the_owners_class_label_a_draft_topic_and_a_retired_class():
    rows = [
        *members("sparse-autoencoders"),
        Membership(slug="sparse-autoencoders", source=Source.OWNER, member=False),
    ]
    assert grading_topics(TREE, rows) == set()  # the owner took it out
    draft = TREE.model_copy(
        update={
            "topics": [
                t.model_copy(update={"status": Status.DRAFT}) if t.slug == "interpretability" else t
                for t in TREE.topics
            ]
        }
    )
    assert grading_topics(draft, members("sparse-autoencoders")) == set()
    assert grading_topics(TREE, members("a-retired-class")) == set()


def test_a_rewritten_paper_keeps_the_topics_its_earlier_text_held_a_grade_in():
    # Classification no longer puts the new text under Interpretability.
    assert grading_topics(TREE, members("robotics"), held=["interpretability"]) == {
        "interpretability"
    }
    assert grading_topics(TREE, [], held=["no-longer-followed"]) == set()


def test_the_grade_table():
    table = {
        (Fit.CENTRAL, Weight.LANDMARK): Grade.MUST_READ,
        (Fit.CENTRAL, Weight.SOLID): Grade.MUST_READ,
        (Fit.CENTRAL, Weight.INCREMENTAL): Grade.RELEVANT,
        (Fit.RELATED, Weight.LANDMARK): Grade.MUST_READ,
        (Fit.RELATED, Weight.SOLID): Grade.RELEVANT,
        (Fit.RELATED, Weight.INCREMENTAL): Grade.ADJACENT,
        (Fit.ADJACENT, Weight.LANDMARK): Grade.ADJACENT,
        (Fit.NONE, Weight.LANDMARK): Grade.NONE,
    }
    for (fit, weight), expected in table.items():
        assert grade(fit, weight) == expected
    assert grade(Fit.RELATED, None) == Grade.RELEVANT  # no Weight yet: solid


def test_the_owners_word_outranks_the_verdict_with_a_floor_at_relevant():
    assert effective(None, Fit.RELATED, Weight.INCREMENTAL).grade == Grade.ADJACENT
    # The owner's related never yields less than relevant; central already does not.
    owned = effective(Fit.RELATED, Fit.NONE, Weight.INCREMENTAL)
    assert (owned.fit, owned.grade) == (Fit.RELATED, Grade.RELEVANT)
    assert effective(Fit.NONE, Fit.CENTRAL, Weight.LANDMARK).grade == Grade.NONE
    assert effective(Fit.ADJACENT, Fit.CENTRAL, Weight.SOLID).grade == Grade.ADJACENT
    assert effective(None, None, Weight.SOLID) is None


def label(value: str, at=T0, provisional=False) -> Label:
    return Label(
        paper_id="p",
        topic="t",
        value=LabelValue(value),
        origin=LabelOrigin.READER,
        provisional=provisional,
        labelled_at=at,
    )


def thumb(reason: str, value="down", at=T0) -> Reaction:
    return Reaction(
        paper_id="p",
        topic="t",
        value=ReactionValue(value),
        reason=ReactionReason(reason),
        reacted_at=at,
    )


def test_the_owners_word_is_a_fit_from_the_newest_label_or_reaction():
    later = T0 + timedelta(hours=1)
    great = thumb("great_find", "up", at=later)
    # Great find: the verdict's Fit if central or related, else related. Wrong topic: none.
    assert owners_word(None, great, Fit.CENTRAL, focus=True) == Fit.CENTRAL
    assert owners_word(None, great, Fit.ADJACENT, focus=True) == Fit.RELATED
    assert owners_word(None, great, None, focus=True) == Fit.RELATED
    assert owners_word(label("related"), thumb("wrong_topic", at=later), None, focus=True) == (
        Fit.NONE
    )
    assert owners_word(label("central", at=later), thumb("wrong_topic"), None, focus=True) == (
        Fit.CENTRAL
    )
    # A bulk-accepted label loses to any thumb that speaks; unsure is no word.
    old = label("central", at=later, provisional=True)
    assert owners_word(old, thumb("wrong_topic"), None, focus=True) == Fit.NONE
    assert owners_word(label("unsure"), None, Fit.CENTRAL, focus=True) is None
    # Reasons that carry no word, and any reaction outside a Focus topic, are only collected.
    assert owners_word(None, thumb("not_interesting"), None, focus=True) is None
    assert owners_word(None, thumb("wrong_topic"), Fit.CENTRAL, focus=False) is None
    assert owners_word(label("adjacent"), None, None, focus=False) == Fit.ADJACENT


def test_rarity_on_a_topic_page():
    page = {
        (Fit.NONE, Weight.SOLID): Rarity.COMMON,
        (Fit.ADJACENT, Weight.LANDMARK): Rarity.UNCOMMON,
        (Fit.RELATED, Weight.SOLID): Rarity.RARE,
        (Fit.CENTRAL, Weight.SOLID): Rarity.EPIC,
        (Fit.CENTRAL, Weight.LANDMARK): Rarity.LEGENDARY,
    }
    for (fit, weight), expected in page.items():
        assert effective(None, fit, weight).rarity == expected


@pytest.mark.parametrize(
    ("levels", "weight", "expected"),
    [
        # Field topics are lowered by two; Focus and Area are not.
        ([("agents", Depth.FIELD, Rarity.EPIC)], Weight.INCREMENTAL, (Rarity.UNCOMMON, "agents")),
        (
            [("agents", Depth.FIELD, Rarity.EPIC), ("interp", Depth.AREA, Rarity.RARE)],
            Weight.INCREMENTAL,
            (Rarity.RARE, "interp"),  # a Field must read gives way to an Area relevant
        ),
        (
            [("agents", Depth.FIELD, Rarity.LEGENDARY), ("interp", Depth.AREA, Rarity.RARE)],
            Weight.INCREMENTAL,
            (Rarity.RARE, "agents"),  # a tie goes to the first in the owner's order
        ),
        ([("cot", Depth.FOCUS, Rarity.LEGENDARY)], Weight.LANDMARK, (Rarity.LEGENDARY, "cot")),
        # The Weight's floor, which is all an ungraded paper has.
        ([("cot", Depth.FOCUS, Rarity.COMMON)], Weight.LANDMARK, (Rarity.RARE, "cot")),
        ([], Weight.LANDMARK, (Rarity.RARE, None)),
        ([], None, (Rarity.UNCOMMON, None)),  # no Weight yet: solid
        ([("agents", Depth.FIELD, Rarity.COMMON)], Weight.INCREMENTAL, (Rarity.COMMON, "agents")),
    ],
)
def test_rarity_in_a_mixed_list(levels, weight, expected):
    assert mixed_rarity(levels, weight) == expected


def test_spend_is_projected_to_the_months_end():
    mid = datetime(2026, 10, 16, tzinfo=UTC)  # 15 of October's 31 days gone
    assert projected(15.0, mid) == pytest.approx(31.0)
    # The first hours of a month count as a day, so one early call does not look like a flood.
    assert projected(1.0, datetime(2026, 10, 1, 3, tzinfo=UTC)) == pytest.approx(31.0)
    (anthropic, openrouter) = spend({"anthropic": 105.0}, {"anthropic": 200, "openrouter": 30}, mid)
    assert (anthropic.provider, anthropic.over) == ("anthropic", True)  # 217 projected
    assert (openrouter.spent, openrouter.over) == (0.0, False)
