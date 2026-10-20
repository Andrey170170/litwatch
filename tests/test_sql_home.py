"""`store` is the only package with SQL: no `execute` call outside it beyond the ones this
list names. The list only shrinks; a call moved into `store` comes off it."""

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "litwatch"

# Calls that predate this check, per file; to move into `store`.
BEFORE_THE_RULE = {
    "api/integrations.py": 1,
    "api/reading.py": 1,
    "events/__init__.py": 1,
    "events/dispatch.py": 4,
    "feedback/calibrate.py": 10,
    "feedback/misses.py": 3,
    "intake/collect.py": 7,
    "jobs/lock.py": 5,
    "jobs/scheduler.py": 3,
}


def test_sql_stays_in_store():
    found = {}
    for path in sorted(SRC.rglob("*.py")):
        name = path.relative_to(SRC).as_posix()
        if name.startswith("store/"):
            continue
        calls = sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany"}
            for node in ast.walk(ast.parse(path.read_text()))
        )
        if calls:
            found[name] = calls
    assert found == BEFORE_THE_RULE
