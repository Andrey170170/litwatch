"""`store` is the only package with SQL: no `execute` or `copy` call outside it beyond the
ones this list names, per function. The list only shrinks; a call moved into `store` comes
off it."""

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "litwatch"

# Calls that predate this check, per module-level function or method; to move into `store`.
BEFORE_THE_RULE = {
    "api/integrations.py:search_outside": 1,
    "api/reading.py:_bibtex": 1,
    "events/__init__.py:emit": 1,
    "events/dispatch.py:deliver": 2,
    "events/dispatch.py:idle": 2,
    "feedback/calibrate.py:_add": 1,
    "feedback/calibrate.py:_all": 1,
    "feedback/calibrate.py:answer": 4,
    "feedback/calibrate.py:draw": 1,
    "feedback/calibrate.py:more": 2,
    "feedback/calibrate.py:session": 1,
    "feedback/misses.py:report": 3,
    "intake/collect.py:_snapshots": 1,
    "intake/collect.py:_state": 3,
    "intake/collect.py:collect": 3,
    "jobs/lock.py:held_elsewhere": 1,
    "jobs/lock.py:job_lock": 2,
    "jobs/lock.py:run_lock": 2,
    "jobs/scheduler.py:_last_run": 1,
    "jobs/scheduler.py:tick": 2,
}

DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _sql(node: ast.AST) -> bool:
    # `copy` with a statement is COPY; without one it is a dict's or a model's copy.
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and (
            node.func.attr in {"execute", "executemany"}
            or (node.func.attr == "copy" and bool(node.args))
        )
    )


def _owners(tree: ast.Module):
    """Each module-level function and method by name, and the module's other statements."""
    for top in tree.body:
        if isinstance(top, DEFS):
            yield top.name, top
        elif isinstance(top, ast.ClassDef):
            yield from ((f"{top.name}.{d.name}", d) for d in top.body if isinstance(d, DEFS))
        else:
            yield "<module>", top


def test_sql_stays_in_store():
    found: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        name = path.relative_to(SRC).as_posix()
        if name.startswith("store/"):
            continue
        for owner, node in _owners(ast.parse(path.read_text())):
            if calls := sum(_sql(n) for n in ast.walk(node)):
                key = f"{name}:{owner}"
                found[key] = found.get(key, 0) + calls
    assert found == BEFORE_THE_RULE
