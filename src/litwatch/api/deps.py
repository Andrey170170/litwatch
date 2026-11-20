"""What every route needs: the app's Database gate, and the shared token (or the read token);
what a route that changes papers settles them by; and the month's spend against budget, which
the status line and Quality both show."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from litwatch.config import Budget
from litwatch.core.budget import Spend, month_start, spend
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate, ReadUnit
from litwatch.store import calls

COOKIE = "litwatch_token"

# What the read token may do: search and read papers and fetch a paper's PDF, as the deep
# service's agent does (its figures for a PDF-only paper are cut from the PDF's pages).
READABLE = {"/api/papers", "/api/papers/{paper_id:path}", "/api/papers/{paper_id:path}/pdf"}


def _same(given: str | None, token: str | None) -> bool:
    return bool(given) and bool(token) and hmac.compare_digest(given.encode(), token.encode())


def token_ok(request: Request, given: str | None) -> bool:
    return _same(given, request.app.state.token)


def authorised(request: Request) -> None:
    """The token as a bearer header or the cookie. Either is enough, so a stale cookie does
    not block a good header. The read token, as a bearer header only, is good for reading
    papers (`READABLE`, GET) and answers 403 everywhere else."""
    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    bearer = bearer if scheme.lower() == "bearer" else None
    if any(token_ok(request, g) for g in (request.cookies.get(COOKIE), bearer)):
        return
    if _same(bearer, request.app.state.read_token):
        route = request.scope.get("route")
        if request.method == "GET" and getattr(route, "path", None) in READABLE:
            return
        raise HTTPException(status_code=403, detail="the read token only reads papers")
    raise HTTPException(status_code=401, detail="missing or wrong token")


async def _db(request: Request) -> DatabaseGate:
    return request.app.state.db


# The app's Database gate, which the app opens and closes in its lifespan: a route opens its
# units on it, or passes it to a Service that takes `db`.
Db = Annotated[DatabaseGate, Depends(_db)]


def _rules(request: Request) -> word.Rules:
    return word.rules(request.app.state.config)


# What settling takes from the app's configuration: for a route that changes papers.
Rules = Annotated[word.Rules, Depends(_rules)]

UNAUTHORISED = {
    401: {"description": "Missing or wrong token"},
    403: {"description": "The read token, outside reading papers"},
}


def protected(prefix: str) -> APIRouter:
    """A router whose every route asks for the token (`authorised`) and documents its refusals."""
    return APIRouter(prefix=prefix, dependencies=[Depends(authorised)], responses=UNAUTHORISED)


def set_token_cookie(request: Request, response: Response, token: str) -> None:
    """The cookie the web app rides on: httpOnly, same-site, and Secure whenever the request
    came over HTTPS (directly, or through a proxy that says so)."""
    https = request.url.scheme == "https" or (
        request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    )
    response.set_cookie(
        COOKIE, token, httponly=True, samesite="strict", secure=https, max_age=90 * 86400
    )


async def budget_lines(unit: ReadUnit, budget: Budget) -> list[Spend]:
    """Month-to-date spend per provider from the call journal, projected to the month's end,
    against `[budget]`."""
    now = datetime.now(UTC)
    return spend(await calls.spent_since(unit, month_start(now)), budget.by_provider, now)
