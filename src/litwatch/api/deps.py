"""What every route needs: a connection, and the shared token."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from typing import Annotated

import psycopg
from fastapi import Depends, HTTPException, Request

from litwatch import store

COOKIE = "litwatch_token"


async def _conn(request: Request) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await store.connect(request.app.state.dsn) as conn:
        yield conn


def token_ok(request: Request, given: str | None) -> bool:
    token = request.app.state.token.encode()
    return bool(given) and hmac.compare_digest(given.encode(), token)


def authorised(request: Request) -> None:
    """The token as a bearer header or the cookie. Either is enough, so a stale cookie does
    not block a good header."""
    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    given = [request.cookies.get(COOKIE), bearer if scheme.lower() == "bearer" else None]
    if not any(token_ok(request, g) for g in given):
        raise HTTPException(status_code=401, detail="missing or wrong token")


Conn = Annotated[psycopg.AsyncConnection, Depends(_conn)]
