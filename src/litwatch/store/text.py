"""Text from outside litwatch as a column stores it: an error a remote end answered, say, or
the JSON a remote service returned."""

from __future__ import annotations

from typing import Any


def stored(text: str, limit: int | None = 2000) -> str:
    """`text` as a text column takes it, at most `limit` characters (None, all of it):
    PostgreSQL refuses a NUL, and the database's encoding a lone surrogate, so the write that
    records a failure would fail itself and leave nothing to retry."""
    return text.replace("\x00", "�").encode("utf-8", "replace").decode("utf-8")[:limit]


def stored_json(value: Any) -> Any:
    """`value` as a JSONB column takes it: every string in it, keys included, `stored` whole,
    since JSONB refuses the same characters a text column does."""
    if isinstance(value, str):
        return stored(value, None)
    if isinstance(value, dict):
        return {stored_json(k): stored_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [stored_json(v) for v in value]
    return value
