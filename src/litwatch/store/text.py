"""Text from outside litwatch as a column stores it: an error a remote end answered, say."""

from __future__ import annotations


def stored(text: str, limit: int = 2000) -> str:
    """`text` as a text column takes it, at most `limit` characters: PostgreSQL refuses a NUL,
    and the database's encoding a lone surrogate, so the write that records a failure would
    fail itself and leave nothing to retry."""
    return text.replace("\x00", "�").encode("utf-8", "replace").decode("utf-8")[:limit]
