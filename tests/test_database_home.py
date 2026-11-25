"""The Database gate is the only door to the database (docs/specs/architecture/a1-database-gate.md):
no advisory-lock SQL outside `gates/database.py`, no connection outside `gates/` and `store/`,
and a test's connection typed, so ty sees one handed where a unit is wanted."""

import ast
import re
from pathlib import Path

from litwatch.gates.database import HELD_ACROSS_CALLS, REGISTRY

TESTS = Path(__file__).parent
SRC = TESTS.parent / "src" / "litwatch"
GATE = "gates/database.py"


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
    assert found == {}


def test_connections_stay_in_the_gate_and_store():
    found = {
        name
        for name, text in _sources()
        if not name.startswith(("gates/", "store/")) and "AsyncConnection" in text
    }
    assert found == set()


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    args = function.args
    return args.posonlyargs + args.args + args.kwonlyargs


def _sessions(module: ast.Module) -> set[str]:
    """The fixtures that give a test's session in `module`: `conn`, and a fixture of the module
    returning or yielding a parameter that is one (a seeded `db`, `world`)."""
    names, grew = {"conn"}, True
    while grew:
        grew = False
        for node in module.body:
            if (
                isinstance(node, FUNCTIONS)
                and node.name not in names
                and any("fixture" in ast.unparse(d) for d in node.decorator_list)
            ):
                given = {arg.arg for arg in _parameters(node)} & names
                for found in ast.walk(node):
                    if isinstance(found, ast.Return | ast.Yield) and (
                        isinstance(found.value, ast.Name) and found.value.id in given
                    ):
                        names.add(node.name)
                        grew = True
                        break
    return names


def test_a_tests_connection_is_typed():
    """ty does not follow a fixture into the tests that take it: a session left unannotated is
    unknown to it, and a store function handed it, instead of a unit (`sessions.in_read`,
    `in_write`), goes unseen. So every parameter named for a fixture that gives a session, in
    any function, nested ones too, says its type. A session under any other name a store
    function is handed raises when it runs (`sessions.SessionInStoreError`)."""
    found = []
    for path in sorted(TESTS.rglob("*.py")):
        module = ast.parse(path.read_text())
        sessions = _sessions(module)
        for node in ast.walk(module):
            if isinstance(node, FUNCTIONS):
                for arg in _parameters(node):
                    if arg.arg in sessions and arg.annotation is None:
                        found.append(f"{path.relative_to(TESTS)}:{node.lineno} {node.name}")
    assert found == []


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
        ("migration", 7213910, "startup", None),
    ]


def test_the_locks_held_across_calls_only_shrink():
    assert set(HELD_ACROSS_CALLS) <= {"run"}
    assert all(lock.scope != "session" or lock.name in HELD_ACROSS_CALLS for lock in REGISTRY)
