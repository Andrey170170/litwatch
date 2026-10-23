"""A job's hard ceiling (models.md, budgets; docs/plans/jobs.md): before each call, retries and
fallbacks included, the gateway reserves the most the call can cost against what is left, and
sends nothing that does not fit; the call's charge replaces the reservation once it is known.

The most a call can cost is its route's price bound (`Bound`) times its input, bounded by the
request's UTF-8 bytes (a token is at least one byte) plus `OVERHEAD` for the provider's own
framing, and its output cap. A route without a bound is not sent under a ceiling."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from litwatch.config import RoleConfig
from litwatch.llm import prices
from litwatch.llm.types import Request

# Tokens a provider adds around a request (tool and schema framing), beyond its bytes.
OVERHEAD = 1024


@dataclass(frozen=True)
class Bound:
    """The most a route charges, in USD per token: each input token and each output token,
    `max_output` the output cap sent when the role sets none (OpenRouter, which has no default;
    None where the route needs none), and `max_price` the per-million prices sent with the
    call (OpenRouter's `provider.max_price`), so no dearer provider serves it."""

    input: float
    output: float
    max_output: int | None = None
    max_price: dict[str, float] | None = None


class CeilingReachedError(Exception):
    """A call did not fit in what is left, with nothing else in flight to free room."""


def route(role: RoleConfig) -> str:
    """A route's key: its provider and model, and whether it is asked on a Decisions endpoint,
    which bills differently."""
    return f"{role.provider}:{role.model}" + (" decisions" if role.decisions else "")


def input_tokens(request: Request) -> int:
    """The most input tokens the request can be: its bytes as sent, plus the framing."""
    material = request.key_material()
    return len(json.dumps(material, ensure_ascii=False).encode()) + OVERHEAD


def anthropic_bound(role: RoleConfig, tokens_in: int) -> Bound | None:
    """`llm.prices` at its worst: every input token at the cache-write price, and the
    long-prompt factor on everything once the input may pass its threshold. None for a model
    without a listed price."""
    if role.model not in prices.ANTHROPIC:
        return None
    price_in, price_out, _ = prices.ANTHROPIC[role.model]
    threshold, factor = prices.LONG_PROMPT.get(role.model, (0, 1.0))
    scale = factor if threshold and tokens_in > threshold else 1.0
    return Bound(scale * price_in * prices.CACHE_WRITE / 1e6, scale * price_out / 1e6)


class Ceiling:
    """What a job may still spend, shared by every call of its gateway. A call that does not
    fit waits while others are in flight, since their charges are usually far below their
    reservations; with none in flight it stops the job, and every call after it is refused."""

    def __init__(self, left: float, bounds: dict[str, Bound | None]) -> None:
        self.left = left  # USD not yet spent or reserved
        self.bounds = bounds  # OpenRouter's, by `route`; a route not listed has none
        self.in_flight = 0
        self.spent = 0.0  # by this gateway's calls, reservations kept for unknown charges
        self.reached = False
        self._room = asyncio.Condition()

    def bound(self, role: RoleConfig, tokens_in: int) -> Bound | None:
        if role.provider == "anthropic":
            return anthropic_bound(role, tokens_in)
        return self.bounds.get(route(role))

    @staticmethod
    def most(bound: Bound, tokens_in: int, max_output: int) -> float:
        return tokens_in * bound.input + max_output * bound.output

    async def reserve(self, amount: float) -> None:
        """Hold `amount` against what is left, waiting while calls in flight may free room.
        Raises `CeilingReachedError` when it cannot fit."""
        async with self._room:
            while not self.reached and amount > self.left and self.in_flight:
                await self._room.wait()
            if self.reached or amount > self.left:
                self.reached = True
                self._room.notify_all()
                raise CeilingReachedError
            self.left -= amount
            self.in_flight += 1

    async def settle(self, reserved: float, cost: float | None) -> None:
        """The call ended: its charge, when known, replaces its reservation; an unknown one
        (a timeout, a reply without a cost) keeps it, as it may have been billed in full."""
        async with self._room:
            charged = reserved if cost is None else cost
            self.left += reserved - charged
            self.spent += charged
            self.in_flight -= 1
            self._room.notify_all()
