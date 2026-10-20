"""Every HTTP status a route answers with is in its OpenAPI responses, so the web client's
generated types know every error it can get. Read from the source: each endpoint, its
dependencies, and the `litwatch.api` functions they call by name or as `module.function`, for
the `HTTPException`s they raise and the responses they return, by their status (given or the
class's default)."""

import ast
import importlib
import inspect
import pkgutil
import textwrap
import types

from fastapi import HTTPException, Response
from fastapi.routing import APIRoute, iter_route_contexts

from litwatch import api
from litwatch.api import create_app

# Answered outside the schema: the web app's fallback, and the monitoring app on its own port.
OUTSIDE_SCHEMA = {"litwatch.api.app._serve_web", "litwatch.api.monitor.create_monitor"}


def _target(fn: types.FunctionType, node: ast.expr) -> object:
    """What a called name or `module.attribute` is, in `fn`'s module."""
    if isinstance(node, ast.Name):
        return fn.__globals__.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        module = fn.__globals__.get(node.value.id)
        if isinstance(module, types.ModuleType):
            return getattr(module, node.attr, None)
    return None


def _raised(fn: types.FunctionType, seen: dict[str, set[int]]) -> None:
    """Into `seen`, keyed by function, the statuses `fn` and the API functions it calls answer."""
    name = f"{fn.__module__}.{fn.__qualname__}"
    if name in seen:
        return
    seen[name] = set()
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
        if not isinstance(node, ast.Call):
            continue
        target = _target(fn, node.func)
        if isinstance(target, type) and issubclass(target, (HTTPException, Response)):
            signature = inspect.signature(target)
            given = {k.arg: k.value for k in node.keywords if k.arg}
            status = signature.bind_partial(*node.args, **given).arguments.get("status_code")
            if status is None:
                status = ast.Constant(signature.parameters["status_code"].default)
            assert isinstance(status, ast.Constant) and isinstance(status.value, int), (
                f"{name}: status {ast.unparse(node)}"
            )
            seen[name].add(status.value)
        elif isinstance(target, types.FunctionType) and target.__module__.startswith(
            "litwatch.api"
        ):
            _raised(target, seen)


def _dependencies(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _dependencies(d)


def _routes():
    """Each schema route and method, with its OpenAPI responses and the code that answers it."""
    app = create_app("postgresql://unused", "unused")
    paths = app.openapi()["paths"]
    for r in iter_route_contexts(app.routes):
        if isinstance(r.route, APIRoute) and r.route.include_in_schema:
            code = [r.endpoint, *_dependencies(r.route.dependant)]
            code = [f for f in code if isinstance(f, types.FunctionType)]
            for method in r.methods or ():
                yield r, code, paths[r.path_format][method.lower()]["responses"]


def test_every_answered_status_is_documented():
    missing = []
    for route, code, documented in _routes():
        seen: dict[str, set[int]] = {}
        for fn in code:
            _raised(fn, seen)
        answered = set().union(*seen.values())
        if undocumented := sorted(answered - {int(status) for status in documented}):
            missing.append(f"{sorted(route.methods)} {route.path_format}: {undocumented}")
    assert not missing


def test_the_walk_reaches_every_status():
    """A status the walk cannot reach from a route (a helper called some other way, an
    exception built ahead) would go unchecked: every API function that answers with one is
    reached, or is outside the schema, and no module holds a ready-made exception or response."""
    reached: dict[str, set[int]] = {}
    for _, code, _ in _routes():
        for fn in code:
            _raised(fn, reached)
    answering: dict[str, set[int]] = {}
    for info in pkgutil.iter_modules(api.__path__, "litwatch.api."):
        module = vars(importlib.import_module(info.name))
        ready = [k for k, v in module.items() if isinstance(v, (HTTPException, Response))]
        assert not ready, f"{info.name}: {ready}"
        for fn in module.values():
            if isinstance(fn, types.FunctionType) and fn.__module__ == info.name:
                _raised(fn, answering)
    unreached = {f for f, statuses in answering.items() if statuses and f not in reached}
    assert unreached == OUTSIDE_SCHEMA
