"""Classification's request from the tree, and what its answer makes a paper a member of."""

from litwatch import classification, seeds
from litwatch.core.classification import (
    Classified,
    band,
    direct,
    grading_topics,
    keyword,
    with_ancestors,
)
from litwatch.llm.decisions import Chosen, Decision

TREE = seeds.taxonomy()


def answer(primary="circuits", **probabilities) -> Classified:
    base = dict.fromkeys((c.slug for c in TREE.classes), 0.0)
    return Classified(probabilities={**base, **probabilities}, primary=primary)


def test_the_request_asks_every_class_and_offers_the_primary_class_by_name():
    request = classification.request(TREE, "A title", "An abstract")
    *per_class, primary = request.questions
    assert [q.name for q in per_class] == [c.slug for c in TREE.classes]
    assert per_class[0].instructions.startswith("Does this arXiv paper belong to Language models:")
    assert [label for label, _ in primary.options] == [c.name for c in TREE.classes]
    decision = Decision(
        probabilities={c.slug: 0.0 for c in TREE.classes},
        choices={primary.name: Chosen(option="Circuits and causal analysis", probability=0.9)},
    )
    assert classification.classified(TREE, decision).primary == "circuits"


def test_the_primary_class_and_every_class_at_its_threshold_are_direct_memberships():
    found = direct(TREE, answer("circuits", steering=0.5, robotics=0.49))
    assert found == {"circuits", "steering"}
    assert with_ancestors(TREE, found) == {"circuits", "steering", "interpretability"}


def test_the_band_brings_a_focus_class_in_from_its_subtree_and_nothing_else():
    assert band(TREE, answer("robotics", circuits=0.3)) == {"circuits"}
    # Below the floor, at the threshold (already a member) and outside a Focus subtree: no.
    assert band(TREE, answer("robotics", circuits=0.1, steering=0.3)) == set()
    assert band(TREE, answer("robotics", circuits=0.5)) == set()


def test_the_keyword_check_matches_focus_patterns_without_a_model():
    hits = keyword(TREE, "Is the chain of thought faithful?", "We test unfaithful reasoning.")
    assert "cot-faithfulness" in hits
    assert keyword(TREE, "Robot grasping", "A gripper.") == {}


def test_each_direct_membership_is_graded_by_the_nearest_followed_class_at_or_above_it():
    # Sparse autoencoders is under Interpretability (Area); CoT faithfulness is followed itself.
    assert grading_topics(TREE, {"sparse-autoencoders"}) == {"interpretability"}
    assert grading_topics(TREE, {"cot-faithfulness"}) == {"cot-faithfulness"}
    assert grading_topics(TREE, {"cot-faithfulness", "sparse-autoencoders"}) == {
        "cot-faithfulness",
        "interpretability",
    }
    assert grading_topics(TREE, {"robotics"}) == set()
