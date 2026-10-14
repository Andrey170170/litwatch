"""The taxonomy's rules and its YAML form, on the shipped seed and on small trees."""

from pathlib import Path

import pytest
import yaml

from litwatch import seeds
from litwatch.core.taxonomy import Depth, Taxonomy


def spec(*, topics=(), deep=False, **overrides):
    """A three-level tree a > b > c, with c followed by default (so it has no description)."""
    leaf = {"slug": "c", "name": "Leaf"}
    if deep:
        leaf["children"] = [{"slug": "d", "name": "Too deep", "description": "Fourth level."}]
    middle = {"slug": "b", "name": "Middle", "description": "A middle class.", "children": [leaf]}
    top = {"slug": "a", "name": "Top", "description": "A top class.", "children": [middle]}
    return {"classes": [top], "topics": list(topics), **overrides}


def topic(cls="c", depth="focus", **extra):
    base = {
        "class": cls,
        "depth": depth,
        "subject": "What the topic covers, at length.",
        "monogram": "TT",
        "colour": "#123456",
    }
    if depth == "focus":
        base["rubric"] = "- Central: the thing itself."
    return {**base, **extra}


def test_the_seed_loads_with_every_class_described_and_every_focus_topic_with_a_rubric():
    tree = seeds.taxonomy()
    assert all(c.description for c in tree.classes)
    assert max(tree.level(c.slug) for c in tree.classes) <= 3
    focus = [t for t in tree.topics if t.depth == Depth.FOCUS]
    assert {t.class_ for t in focus} == {"cot-faithfulness", "cot-monitoring", "circuits"}
    assert all(t.rubric and t.keywords for t in focus)
    # A followed class is described by its topic's subject.
    assert tree.get("circuits").description == tree.topic("circuits").subject


def test_a_taxonomy_round_trips_through_yaml():
    tree = seeds.taxonomy()
    assert seeds.taxonomy_from_spec(yaml.safe_load(seeds.dump_taxonomy(tree))) == tree


def test_a_small_tree_flattens_parents_first():
    tree = seeds.taxonomy_from_spec(spec(topics=[topic()]))
    assert [(c.slug, c.parent) for c in tree.classes] == [("a", None), ("b", "a"), ("c", "b")]
    assert tree.get("c").description == "What the topic covers, at length."


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        (spec(deep=True, topics=[topic()]), "more than 3 levels deep"),
        (
            spec(
                classes=[
                    {"slug": "a", "name": "Same", "description": "One class."},
                    {"slug": "b", "name": "same", "description": "Another class."},
                ]
            ),
            "used 2 times",
        ),
        (spec(topics=[topic(), topic(cls="missing")]), "no such class"),
        (spec(topics=[topic(rubric=None)]), "has no rubric"),
        (spec(topics=[topic(depth="area", rubric="- Central: x.")]), "only Focus topics"),
        (spec(topics=[topic(), topic()]), "followed 2 times"),
        ({}, "at least 1 item"),
        (spec(topics=[topic(), {}]), "Field required"),
        ({"classes": ["not a class"]}, "valid dictionary"),
    ],
)
def test_a_broken_taxonomy_is_refused_with_its_problem(raw, problem):
    with pytest.raises(ValueError, match=problem):
        seeds.taxonomy_from_spec(raw)


def test_a_followed_class_may_not_carry_its_own_description():
    raw = spec(topics=[topic(), topic(cls="b")])
    with pytest.raises(ValueError, match="its description is its topic's subject"):
        seeds.taxonomy_from_spec(raw)


def test_a_followed_class_described_apart_from_its_subject_is_refused():
    tree = seeds.taxonomy_from_spec(spec(topics=[topic()]))
    classes = [c.model_copy(update={"description": "Something else entirely."}) if c.slug == "c"
               else c for c in tree.classes]  # fmt: skip
    with pytest.raises(ValueError, match="its description must be its subject"):
        Taxonomy(classes=classes, topics=tree.topics)


def test_a_topic_directory_import_skips_the_taxonomy_file():
    root = Path(seeds.__file__).parent
    assert [t.slug for t in seeds.load(root)] == [t.slug for t in seeds.starter()]
    assert len(seeds.starter()) == 7
