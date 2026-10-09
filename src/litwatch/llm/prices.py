"""Prices for providers that report tokens and no charge."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# USD per million tokens: input, output, and the cache read price as a fraction of input.
ANTHROPIC: dict[str, tuple[float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.1),
    "claude-sonnet-5-5": (2.0, 10.0, 0.05),
    "claude-opus-5-5": (4.0, 20.0, 0.05),
}
CACHE_WRITE = 1.25  # the five-minute cache, as a multiple of input
# Haiku 5.5 charges a longer prompt five times as much, on every kind of token.
LONG_PROMPT = {"claude-haiku-5-5": (100_000, 5.0)}


def anthropic_cost(
    model: str, fresh: int, cache_read: int, cache_write: int, output: int
) -> float | None:
    """The call's charge, or None for a model without a listed price."""
    if model not in ANTHROPIC:
        log.warning("no price listed for %s, its cost is not recorded", model)
        return None
    price_in, price_out, read = ANTHROPIC[model]
    threshold, factor = LONG_PROMPT.get(model, (0, 1.0))
    scale = factor if threshold and fresh + cache_read + cache_write > threshold else 1.0
    tokens_in = fresh + cache_read * read + cache_write * CACHE_WRITE
    return scale * (tokens_in * price_in + output * price_out) / 1e6
