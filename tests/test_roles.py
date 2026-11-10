"""The role rules over the import graph (docs/specs/architecture.md, "Layout and enforcement").
`lint-imports` checks which role may import which, by the contracts in `pyproject.toml`; this
file keeps those contracts' package lists in step with the packages, and checks that no two
Service packages import each other in a cycle, function-level imports included. The cycle list
only shrinks: an import that leaves a cycle comes off it."""

import tomllib
from collections import deque
from functools import cache
from pathlib import Path

import grimp
import pytest

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

# Every top-level module that is not a Service; every other one is.
NOT_SERVICES = {
    "core": "Records and Policies",
    "store": "Repositories",
    "llm": "the Model gate",
    "api": "Entry",
    "cli": "Entry",
    "config": "shared",
    "logs": "shared",
    "concurrency": "shared",
    "writing": "shared",
}
ENTRIES = {"api", "cli"}

# Imports from one Service package into another that lie on a cycle, before the rule: the
# imported package imports back, directly or through other Services. To be broken.
CYCLES_BEFORE_THE_RULE = {
    ("digests", "jobs"),  # digests -> jobs.context
    ("feedback", "intake"),  # feedback.misses -> intake.batch, intake.search
    ("fulltext", "jobs"),  # fulltext -> jobs.context, jobs.lock; fulltext.deep -> jobs.worker
    ("intake", "feedback"),  # intake.batch -> feedback.word
    ("jobs", "digests"),  # jobs.digests, jobs.nightly -> digests
    ("jobs", "fulltext"),  # jobs.nightly, jobs.reruns -> fulltext
    ("jobs", "quality"),  # jobs.replays, jobs.worker -> quality.replay
    ("quality", "digests"),  # quality.ladders -> digests.writer
    ("quality", "fulltext"),  # quality.briefs, quality.ladders -> fulltext.brief, fulltext.trim
    ("quality", "jobs"),  # quality.replay -> jobs.context
}


@cache
def _graph() -> grimp.ImportGraph:
    return grimp.build_graph("litwatch", cache_dir=None)


def _top(module: str) -> str:
    """The top-level module under `litwatch` that a module belongs to; "" for `litwatch` itself."""
    return module.split(".")[1] if "." in module else ""


def _packages() -> set[str]:
    return {_top(m) for m in _graph().find_children("litwatch")}


def _services() -> set[str]:
    return _packages() - set(NOT_SERVICES)


def _contract(name: str) -> dict:
    contracts = tomllib.loads(PYPROJECT.read_text())["tool"]["importlinter"]["contracts"]
    (contract,) = (c for c in contracts if c.get("source_modules") == [f"litwatch.{name}"])
    return contract


def _forbidden(name: str) -> set[str]:
    return {_top(m) for m in _contract(name)["forbidden_modules"]}


def test_contracts_name_every_package():
    assert set(NOT_SERVICES) <= _packages()
    assert _forbidden("store") == _packages() - {"store", "core"}
    assert _forbidden("llm") == _services() | ENTRIES


def _service_imports() -> dict[tuple[str, str], list[str]]:
    """Each import from one Service package into another, with its lines."""
    graph, services = _graph(), _services()
    found: dict[tuple[str, str], list[str]] = {}
    for importer in sorted(graph.modules):
        for imported in sorted(graph.find_modules_directly_imported_by(importer)):
            a, b = _top(importer), _top(imported)
            if a != b and {a, b} <= services:
                found.setdefault((a, b), []).extend(
                    f"{d['importer']}:{d['line_number']}: {d['line_contents']}"
                    for d in graph.get_import_details(importer=importer, imported=imported)
                )
    return found


def _path(edges: set[tuple[str, str]], start: str, end: str) -> list[str] | None:
    """The shortest path from one package to another along the edges, if there is one."""
    came_from: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        at = queue.popleft()
        if at == end:
            path = [at]
            while (at := came_from[at]) is not None:
                path.append(at)
            return path[::-1]
        for a, b in sorted(edges):
            if a == at and b not in came_from:
                came_from[b] = at
                queue.append(b)
    return None


def test_services_form_no_cycle():
    imports = _service_imports()
    edges = set(imports)
    on_cycle = {(a, b): _path(edges, b, a) for a, b in edges}
    on_cycle = {edge: back for edge, back in on_cycle.items() if back}

    problems = [
        f"{a} -> {' -> '.join(on_cycle[a, b])} is a cycle between Services; break it at:\n"
        + "\n".join(f"    {line}" for line in sorted(set(imports[a, b])))
        for a, b in sorted(on_cycle.keys() - CYCLES_BEFORE_THE_RULE)
    ] + [
        f"{a} -> {b} is no longer on a cycle; take it off CYCLES_BEFORE_THE_RULE"
        for a, b in sorted(CYCLES_BEFORE_THE_RULE - on_cycle.keys())
    ]
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)
