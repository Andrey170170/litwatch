"""Prices for providers that report tokens and no charge: Anthropic and OpenAI."""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# USD per million tokens: input, output, and the cache read price as a fraction of input.
ANTHROPIC: dict[str, tuple[float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.1),
    "claude-sonnet-5-5": (2.0, 10.0, 0.05),
    "claude-opus-5-5": (4.0, 20.0, 0.05),
}
CACHE_WRITE = 1.25  # the five-minute cache, as a multiple of input
BATCH = 0.5  # Message Batches charge half of every price
# Haiku 5.5 charges a longer prompt five times as much, on every kind of token.
LONG_PROMPT = {"claude-haiku-5-5": (100_000, 5.0)}


def anthropic_cost(
    model: str, fresh: int, cache_read: int, cache_write: int, output: int, *, batch: bool = False
) -> float | None:
    """The call's charge, or None for a model without a listed price; half of it in a batch."""
    if model not in ANTHROPIC:
        log.warning("no price listed for %s, its cost is not recorded", model)
        return None
    price_in, price_out, read = ANTHROPIC[model]
    threshold, factor = LONG_PROMPT.get(model, (0, 1.0))
    scale = factor if threshold and fresh + cache_read + cache_write > threshold else 1.0
    tokens_in = fresh + cache_read * read + cache_write * CACHE_WRITE
    scale *= BATCH if batch else 1.0
    return scale * (tokens_in * price_in + output * price_out) / 1e6


# USD per million tokens at OpenAI's Standard rates for a prompt of up to 272K input tokens:
# input, output, and the cached input price as a fraction of input. From
# developers.openai.com/api/docs/pricing ("Standard pricing data"), read 2026-11-23.
OPENAI: dict[str, tuple[float, float, float]] = {
    "gpt-6.2-astra": (8.0, 40.0, 0.05),
    "gpt-6.2-sol": (2.0, 10.0, 0.05),
    "gpt-6.1-sol": (2.0, 10.0, 0.05),
    "gpt-6.1-terra": (1.0, 5.0, 0.05),
    "gpt-6-luna": (0.10, 0.50, 0.1),
}
OPENAI_CACHE_WRITE = 1.25  # the default 30-minute cache, as a multiple of input
# Past 272K input tokens the whole request is charged more: input and cache rates twice, output
# 1.5 times (the same page, "Long context"). The Batch API charges half of every rate (BATCH).
OPENAI_LONG_PROMPT = (272_000, 2.0, 1.5)
# A model answers with its snapshot's name (`gpt-6.2-sol-2026-11-01`); its price is the alias's.
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def openai_model(model: str) -> str:
    """The model's alias, without a snapshot's date."""
    return _SNAPSHOT.sub("", model)


def openai_cost(
    model: str, tokens_in: int, cache_read: int, cache_write: int, output: int, *, batch: bool
) -> float | None:
    """The call's charge, or None for a model without a listed price. `tokens_in` counts every
    input token; the cache's reads and writes are part of it (OpenAI's prompt-caching guide)."""
    model = openai_model(model)
    if model not in OPENAI:
        log.warning("no price listed for %s, its cost is not recorded", model)
        return None
    price_in, price_out, read = OPENAI[model]
    threshold, scale_in, scale_out = OPENAI_LONG_PROMPT
    if tokens_in <= threshold:
        scale_in = scale_out = 1.0
    fresh = tokens_in - cache_read - cache_write
    cost = (fresh + cache_read * read + cache_write * OPENAI_CACHE_WRITE) * price_in * scale_in
    cost += output * price_out * scale_out
    return (BATCH if batch else 1.0) * cost / 1e6
