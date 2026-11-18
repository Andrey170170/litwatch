"""The Database gate is the only door to the database (docs/specs/architecture/a1-database-gate.md):
no advisory-lock SQL outside `gates/database.py`, and no connection outside `gates/` and
`store/`. Each list below names what predates the rule and only shrinks: code moved onto the
gate comes off it, and the lists are empty when A1 is done."""

import re
from pathlib import Path

from litwatch.gates.database import HELD_ACROSS_CALLS, REGISTRY
from litwatch.store import taxonomy

SRC = Path(__file__).parent.parent / "src" / "litwatch"
GATE = "gates/database.py"

# Mentions of an advisory-lock function per file, before the rule; to move into the gate. The
# taxonomy lock's raw-connection branch goes in P6, with the last caller passing a connection.
LOCKS_BEFORE_THE_RULE = {
    "store/taxonomy.py": 2,
}

# Modules outside `gates/` and `store/` that name `AsyncConnection`, before the rule; each is to
# take `db` or a unit instead.
CONNECTIONS_BEFORE_THE_RULE = {
    "digests/__init__.py",
    "events/__init__.py",
    "feedback/calibrate.py",
    "feedback/misses.py",
    "intake/archive.py",
    "intake/catalog.py",
    "intake/collect.py",
    "integrations/__init__.py",
    "integrations/archive.py",
    "integrations/notion.py",
    "integrations/webdav.py",
    "integrations/webhook.py",
    "integrations/zotero.py",
    "jobs/digests.py",
    "quality/ladders.py",
    "quality/replay.py",
}


def _sources():
    """Each Python and SQL file of the package, by its path under it, with its text."""
    for path in sorted(SRC.rglob("*")):
        if path.suffix in (".py", ".sql"):
            yield path.relative_to(SRC).as_posix(), path.read_text()


def test_advisory_locks_stay_in_the_gate():
    found = {
        name: len(mentions)
        for name, text in _sources()
        if name != GATE and (mentions := re.findall(r"pg_(?:try_)?advisory", text))
    }
    assert found == LOCKS_BEFORE_THE_RULE


def test_connections_stay_in_the_gate_and_store():
    found = {
        name
        for name, text in _sources()
        if not name.startswith(("gates/", "store/")) and "AsyncConnection" in text
    }
    assert found == CONNECTIONS_BEFORE_THE_RULE


def test_the_registry_has_todays_keys():
    """The keys the code took before the gate, made the same way, so a process from before it
    and one after exclude each other while a deploy replaces one with the other."""
    assert [(lock.name, lock.key, lock.scope, lock.arg) for lock in REGISTRY] == [
        ("taxonomy", 7213913, "unit", None),
        ("paper", 7213912, "unit", "text"),
        ("publication", 7213914, "unit", "text"),
        ("rerun", 7213916, "unit", "text"),
        ("replay", 7213917, "unit", None),
        ("retally", 7213912, "unit", "int"),
        ("run", 7213911, "session", None),
        ("schedule", 7213913, "session", "text"),
        ("dispatch", 7213914, "session", "text"),
        ("deep-worker", 7213915, "session", None),
        ("migration", 7213910, "startup", None),
    ]
    # The taxonomy lock's own code still takes it on a raw connection, until P6.
    assert next(lock.key for lock in REGISTRY if lock.name == "taxonomy") == taxonomy.TAXONOMY_LOCK


def test_the_locks_held_across_calls_only_shrink():
    assert set(HELD_ACROSS_CALLS) <= {"run", "schedule", "dispatch", "deep-worker"}
    assert all(lock.scope != "session" or lock.name in HELD_ACROSS_CALLS for lock in REGISTRY)
