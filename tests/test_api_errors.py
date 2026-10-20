"""Every HTTP status a route raises is in its OpenAPI responses, so the web client's generated
types know every error it can get. Read from the source: each endpoint, and the `litwatch.api`
functions it calls by name or as `module.function`, for `HTTPException(status_code=...)`."""

import ast
import importlib
import inspect
import pkgutil
import textwrap
import types

from fastapi.routing import APIRoute, iter_route_contexts

from litwatch import api
from litwatch.api import create_app

# Raised outside any route's own code: `authorised` for every protected route, and the web
# app's fallback, which is not in the schema.
APP_WIDE = {"litwatch.api.deps.authorised", "litwatch.api.app._serve_web"}


def _raised(fn: types.FunctionType, seen: dict[str, set[int]]) -> None:
    """Into `seen`, keyed by function, the statuses `fn` and the API functions it calls raise."""
    name = f"{fn.__module__}.{fn.__qualname__}"
    if name in seen:
        return
    seen[name] = set()
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == "HTTPException":
            status = next((k.value for k in node.keywords if k.arg == "status_code"), None)
            status = status or (node.args[0] if node.args else None)
            assert isinstance(status, ast.Constant), f"{name}: status {ast.unparse(node)}"
            seen[name].add(int(status.value))
            continue
        target = None
        if isinstance(f, ast.Name):
            target = fn.__globals__.get(f.id)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            module = fn.__globals__.get(f.value.id)
            if isinstance(module, types.ModuleType):
                target = getattr(module, f.attr, None)
        if isinstance(target, types.FunctionType) and target.__module__.startswith("litwatch.api"):
            _raised(target, seen)


def _routes():
    app = create_app("postgresql://unused", "unused")
    paths = app.openapi()["paths"]
    for r in iter_route_contexts(app.routes):
        if isinstance(r.route, APIRoute) and r.route.include_in_schema:
            for method in r.methods or ():
                yield r, paths[r.path_format][method.lower()]["responses"]


def test_every_raised_status_is_documented():
    missing = []
    for route, documented in _routes():
        seen: dict[str, set[int]] = {}
        _raised(route.endpoint, seen)
        raised = set().union(*seen.values())
        if undocumented := sorted(raised - {int(code) for code in documented}):
            missing.append(f"{sorted(route.methods)} {route.path_format}: {undocumented}")
    assert not missing


def test_the_walk_reaches_every_raise():
    """A raise the walk cannot reach from a route (a helper called some other way) would
    go unchecked: every API function that raises is reached, or is app-wide."""
    reached: dict[str, set[int]] = {}
    for route, _ in _routes():
        _raised(route.endpoint, reached)
    raising: dict[str, set[int]] = {}
    for info in pkgutil.iter_modules(api.__path__, "litwatch.api."):
        for fn in vars(importlib.import_module(info.name)).values():
            if isinstance(fn, types.FunctionType) and fn.__module__ == info.name:
                _raised(fn, raising)
    unreached = {f for f, codes in raising.items() if codes and f not in reached}
    assert unreached == APP_WIDE
