"""The taxonomy: the class tree and its followed topics, with their revisions.

A save replaces the whole taxonomy. A class or topic gets a new revision, and a version row,
only when its hash changes (`core.taxonomy`); edits apply forward and nothing recorded earlier
is touched. A class missing from the save is retired: its row stays, out of the tree, so what
was recorded against it still resolves. A topic missing from it is unfollowed: its row goes and
its versions stay, so following the class again goes on from its last revision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from litwatch.core import Status, Taxonomy, TaxonomyClass, Topic
from litwatch.gates.database import ReadUnit, WriteUnit


@dataclass
class Saved:
    """What a save changed, by slug."""

    revised_classes: list[str] = field(default_factory=list)  # new, or a new revision
    retired_classes: list[str] = field(default_factory=list)
    revised_topics: list[str] = field(default_factory=list)  # new, or a new revision
    unfollowed_topics: list[str] = field(default_factory=list)
    # Followed, unfollowed, with a new status, depth or revision: what their papers' grades,
    # rarity and full-text passes rest on. A draft topic is not followed; a paused one still
    # is, but queues no full-text pass.
    changed_topics: list[str] = field(default_factory=list)
    # The owner's order of the topics kept changed: a tie between topics now breaks otherwise.
    reordered: bool = False


def require_exclusive(unit: WriteUnit) -> None:
    """Refuse an edit of classes or topics in a unit that does not hold the taxonomy lock
    exclusively, which the gate took when the unit opened (`taxonomy="exclusive"`). Holding it,
    the edit waited for the settles in flight, which hold it shared (a change's unit,
    `feedback.word.change`)."""
    if unit.taxonomy != "exclusive":
        raise RuntimeError(f"unit {unit.name!r} edits the taxonomy without its lock")


def require_lock(unit: ReadUnit) -> None:
    """Refuse a read of the tree in a write unit that does not hold the taxonomy lock, shared
    or exclusively: holding it, no edit commits before the unit does, so every read in it sees
    one version of the tree. A read unit needs none, since it reads one snapshot."""
    if isinstance(unit, WriteUnit) and unit.taxonomy is None:
        raise RuntimeError(f"unit {unit.name!r} reads the taxonomy without its lock")


def row_to_class(row: dict[str, Any]) -> TaxonomyClass:
    return TaxonomyClass(
        slug=row["slug"],
        name=row["name"],
        description=row["description"],
        parent=row["parent"],
        threshold=row["threshold"],
    )


def _topic(row: dict[str, Any]) -> Topic:
    return Topic(
        slug=row["class"],
        depth=row["depth"],
        monogram=row["monogram"],
        colour=row["colour"],
        status=row["status"],
        subject=row["subject"],
        rubric=row["rubric"],
        interests=row["interests"],
        keywords=list(row["keywords"] or []),
    )


async def topics(unit: ReadUnit, status: Status | None = None) -> list[Topic]:
    """The followed topics with the given status (all for None), in the owner's order."""
    rows = await (
        await unit.execute(
            "SELECT * FROM topics WHERE %(s)s::text IS NULL OR status = %(s)s "
            "ORDER BY position, class",
            {"s": status.value if status else None},
        )
    ).fetchall()
    return [_topic(r) for r in rows]


async def load(unit: ReadUnit) -> Taxonomy | None:
    """The current taxonomy, None before one is saved."""
    require_lock(unit)
    rows = await (
        await unit.execute("SELECT * FROM classes WHERE retired_at IS NULL ORDER BY position")
    ).fetchall()
    if not rows:
        return None
    return Taxonomy(classes=[row_to_class(r) for r in rows], topics=await topics(unit))


async def _latest(unit: ReadUnit, table: str) -> dict[str, dict[str, Any]]:
    """The last version row of every class or topic ever saved: its revision and hash."""
    rows = await (
        await unit.execute(
            f"SELECT DISTINCT ON (class) class, revision, hash FROM {table} "
            "ORDER BY class, revision DESC"
        )
    ).fetchall()
    return {r["class"]: r for r in rows}


def _revision(latest: dict[str, dict[str, Any]], slug: str, version: str) -> tuple[int, bool]:
    """The revision for content with this hash, and whether it is a new one."""
    last = latest.get(slug)
    if last is not None and last["hash"] == version:
        return last["revision"], False
    return (last["revision"] if last else 0) + 1, True


async def _add_version(
    unit: WriteUnit,
    table: str,
    slug: str,
    revision: int,
    version: str,
    content: dict[str, Any],
) -> None:
    await unit.execute(
        f"INSERT INTO {table} (class, revision, hash, content) VALUES (%s, %s, %s, %s)",
        (slug, revision, version, Jsonb(content)),
    )


async def save(unit: WriteUnit, tree: Taxonomy) -> Saved:
    """Store `tree` as the whole taxonomy: classes in its order, topics in its order."""
    saved = Saved()
    require_exclusive(unit)
    before = {
        r["class"]: (r["depth"], r["status"], r["revision"])
        for r in await (
            await unit.execute(
                "SELECT class, depth, status, revision FROM topics ORDER BY position, class"
            )
        ).fetchall()
    }

    latest = await _latest(unit, "class_versions")
    for position, c in enumerate(tree.classes, 1):
        revision, new = _revision(latest, c.slug, c.version)
        await unit.execute(
            """
            INSERT INTO classes (slug, parent, name, description, threshold, position,
                                 version, revision)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET
                parent = EXCLUDED.parent, name = EXCLUDED.name,
                description = EXCLUDED.description, threshold = EXCLUDED.threshold,
                position = EXCLUDED.position, version = EXCLUDED.version,
                revision = EXCLUDED.revision, retired_at = NULL, updated_at = now()
            """,
            (
                c.slug,
                c.parent,
                c.name,
                c.description,
                c.threshold,
                position,
                c.version,
                revision,
            ),
        )
        if new:
            content = c.model_dump(mode="json", exclude={"slug"})
            await _add_version(unit, "class_versions", c.slug, revision, c.version, content)
            saved.revised_classes.append(c.slug)

    latest = await _latest(unit, "topic_versions")
    after: dict[str, tuple[str, bool, int]] = {}
    for position, t in enumerate(tree.topics, 1):
        revision, new = _revision(latest, t.slug, t.version)
        after[t.slug] = (t.depth.value, t.status.value, revision)
        await unit.execute(
            """
            INSERT INTO topics (class, depth, subject, rubric, interests, keywords, monogram,
                                colour, position, status, version, revision)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (class) DO UPDATE SET
                depth = EXCLUDED.depth, subject = EXCLUDED.subject,
                rubric = EXCLUDED.rubric, interests = EXCLUDED.interests,
                keywords = EXCLUDED.keywords, monogram = EXCLUDED.monogram,
                colour = EXCLUDED.colour, position = EXCLUDED.position,
                status = EXCLUDED.status, version = EXCLUDED.version,
                revision = EXCLUDED.revision, updated_at = now()
            """,
            (
                t.slug,
                t.depth.value,
                t.subject,
                t.rubric,
                t.interests,
                t.keywords,
                t.monogram,
                t.colour,
                position,
                t.status.value,
                t.version,
                revision,
            ),
        )
        if new:
            content = t.model_dump(
                mode="json", include={"depth", "subject", "rubric", "interests", "keywords"}
            )
            await _add_version(unit, "topic_versions", t.slug, revision, t.version, content)
            saved.revised_topics.append(t.slug)

    kept = list(after)
    gone = await unit.execute("DELETE FROM topics WHERE class <> ALL(%s) RETURNING class", (kept,))
    saved.unfollowed_topics = sorted(r["class"] for r in await gone.fetchall())
    retired = await unit.execute(
        "UPDATE classes SET retired_at = now() WHERE retired_at IS NULL "
        "AND slug <> ALL(%s) RETURNING slug",
        ([c.slug for c in tree.classes],),
    )
    saved.retired_classes = sorted(r["slug"] for r in await retired.fetchall())
    saved.changed_topics = sorted(
        s for s in set(before) | set(after) if before.get(s) != after.get(s)
    )
    saved.reordered = [s for s in before if s in after] != [s for s in after if s in before]
    return saved


async def set_order(unit: WriteUnit, slugs: list[str]) -> None:
    """The owner's order of the topics. `slugs` must name every followed topic exactly once;
    ValueError otherwise. The order is not part of a topic's version."""
    require_exclusive(unit)
    rows = await (await unit.execute("SELECT class FROM topics")).fetchall()
    if sorted(slugs) != sorted(r["class"] for r in rows):
        raise ValueError("name every topic exactly once")
    for position, slug in enumerate(slugs, 1):
        await unit.execute(
            "UPDATE topics SET position = %s, updated_at = now() WHERE class = %s",
            (position, slug),
        )
