"""The taxonomy's rules, its YAML form and its store, on the shipped seed and on small trees."""

import pytest
import yaml

from litwatch import seeds
from litwatch.core import Status
from litwatch.core.taxonomy import Depth, Taxonomy
from litwatch.store import taxonomy as taxonomy_store
from tests.conftest import AGENTS, FAITH, INTERP, TREE


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
    assert {t.slug for t in focus} == {"cot-faithfulness", "cot-monitoring", "circuits"}
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
        (spec(topics=[topic(colour="#123456", color="#654321")]), "Extra inputs"),
        (
            {"classes": [{"slug": "a", "name": "Top", "description": "A top class.", "child": []}]},
            "Extra inputs",
        ),
        (spec(topic=[topic()]), "unknown taxonomy keys"),
        ({"classes": [], "topics": 1}, "`topics` is a list"),
        ({"classes": True}, "`classes` is a list"),
        (
            {"classes": [{"slug": "a", "name": "Top", "description": "A class.", "children": 1}]},
            "`children` is a list",
        ),
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


REWORDED = "Whether a trace leaves out what the model did."


# -- in the database -------------------------------------------------------------------------------


async def _versions(conn, table: str) -> list[tuple[str, int]]:
    rows = await (
        await conn.execute(f"SELECT class, revision FROM {table} ORDER BY 1, 2")
    ).fetchall()
    return [(r["class"], r["revision"]) for r in rows]


async def _revision(conn, table: str, slug: str) -> int:
    key = "slug" if table == "classes" else "class"
    row = await (
        await conn.execute(f"SELECT revision FROM {table} WHERE {key} = %s", (slug,))
    ).fetchone()
    return row["revision"]


async def test_the_seed_round_trips_through_the_database_and_its_yaml(conn):
    assert await taxonomy_store.load(conn) is None
    tree = seeds.taxonomy()
    saved = await taxonomy_store.save(conn, tree)
    assert saved.revised_classes == [c.slug for c in tree.classes]
    assert saved.revised_topics == [t.slug for t in tree.topics]
    assert saved.changed_topics == sorted(t.slug for t in tree.topics)
    assert await taxonomy_store.load(conn) == tree
    exported = seeds.dump_taxonomy(await taxonomy_store.load(conn))
    assert seeds.taxonomy_from_spec(yaml.safe_load(exported)) == tree
    assert len(await _versions(conn, "class_versions")) == len(tree.classes)
    assert await _versions(conn, "topic_versions") == sorted((t.slug, 1) for t in tree.topics)


async def test_a_revision_moves_only_when_its_hash_changes(conn):
    await taxonomy_store.save(conn, TREE)
    again = await taxonomy_store.save(conn, TREE)
    assert again == taxonomy_store.Saved()

    # Badge, status and order are not part of a topic's version; a new status settles the
    # topic's papers (a paused topic queues no full-text pass).
    restyled = FAITH.model_copy(update={"colour": "#000000", "status": Status.PAUSED})
    tree = TREE.following(restyled).model_copy(update={"topics": [INTERP, restyled, AGENTS]})
    assert await taxonomy_store.save(conn, tree) == taxonomy_store.Saved(
        reordered=True, changed_topics=[FAITH.slug]
    )
    assert [t.slug for t in await taxonomy_store.topics(conn)] == [
        INTERP.slug,
        FAITH.slug,
        "agents",
    ]
    assert [t.slug for t in await taxonomy_store.topics(conn, Status.PAUSED)] == [FAITH.slug]

    # A new rubric revises the topic; a new subject revises the topic and its class.
    saved = await taxonomy_store.save(
        conn, TREE.following(FAITH.model_copy(update={"rubric": "- Central: another rubric."}))
    )
    assert (saved.revised_topics, saved.revised_classes) == ([FAITH.slug], [])
    reworded = FAITH.model_copy(
        update={"subject": "Whether a trace leaves out what the model did."}
    )
    saved = await taxonomy_store.save(conn, TREE.following(reworded))
    assert (saved.revised_topics, saved.revised_classes) == ([FAITH.slug], [FAITH.slug])
    assert await _revision(conn, "topics", FAITH.slug) == 3
    assert await _revision(conn, "classes", FAITH.slug) == 2

    # A class's threshold is part of its version.
    current = await taxonomy_store.load(conn)
    classes = [
        c.model_copy(update={"threshold": 0.7}) if c.slug == "agents" else c
        for c in current.classes
    ]
    saved = await taxonomy_store.save(conn, current.model_copy(update={"classes": classes}))
    assert saved.revised_classes == ["agents"] and await _revision(conn, "classes", "agents") == 2
    content = await (
        await conn.execute(
            "SELECT content FROM class_versions WHERE class = 'agents' AND revision = 2"
        )
    ).fetchone()
    assert content["content"]["threshold"] == 0.7


async def test_a_class_left_out_is_retired_and_a_topic_left_out_unfollowed(conn):
    await taxonomy_store.save(conn, TREE)
    smaller = Taxonomy(
        classes=[c for c in TREE.classes if c.slug != FAITH.slug],
        topics=[INTERP, AGENTS],
    )
    saved = await taxonomy_store.save(conn, smaller)
    assert (saved.retired_classes, saved.unfollowed_topics) == ([FAITH.slug], [FAITH.slug])
    assert saved.changed_topics == [FAITH.slug]
    assert await taxonomy_store.load(conn) == smaller
    # The rows a record may name stay: the class (retired) and every version of both.
    row = await (
        await conn.execute("SELECT retired_at FROM classes WHERE slug = %s", (FAITH.slug,))
    ).fetchone()
    assert row["retired_at"] is not None
    assert (FAITH.slug, 1) in await _versions(conn, "topic_versions")

    # Saved again unchanged, both come back at the revision they had.
    saved = await taxonomy_store.save(conn, TREE)
    assert (saved.revised_classes, saved.revised_topics) == ([], [])
    assert saved.changed_topics == [FAITH.slug]
    assert await taxonomy_store.load(conn) == TREE
    assert await _revision(conn, "topics", FAITH.slug) == 1

    # Unfollowed and followed again with another rubric, it goes on from its last revision.
    await taxonomy_store.save(conn, smaller.following(INTERP))
    await taxonomy_store.save(
        conn, TREE.following(FAITH.model_copy(update={"rubric": "- Central: another rubric."}))
    )
    assert await _revision(conn, "topics", FAITH.slug) == 2


async def test_the_owners_order_names_every_topic_once(conn):
    await taxonomy_store.save(conn, TREE)
    await taxonomy_store.set_order(conn, ["agents", FAITH.slug, INTERP.slug])
    assert [t.slug for t in (await taxonomy_store.load(conn)).topics] == [
        "agents",
        FAITH.slug,
        INTERP.slug,
    ]
    for wrong in (["agents", FAITH.slug], ["agents", "agents", FAITH.slug, INTERP.slug]):
        with pytest.raises(ValueError, match="every topic exactly once"):
            await taxonomy_store.set_order(conn, wrong)


def test_following_a_class_describes_it_by_the_topic_and_keeps_the_owners_order():
    reworded = FAITH.model_copy(
        update={"subject": "Whether a trace leaves out what the model did."}
    )
    tree = TREE.following(reworded)
    assert [t.slug for t in tree.topics] == [t.slug for t in TREE.topics]
    assert tree.get(FAITH.slug).description == reworded.subject
    other = AGENTS.model_copy(update={"slug": "other", "monogram": "OT"})
    assert [t.slug for t in TREE.following(other).topics][-1] == "other"
    with pytest.raises(ValueError, match="no such class"):
        TREE.following(AGENTS.model_copy(update={"slug": "missing"}))
