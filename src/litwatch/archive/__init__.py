"""The archive client: arxiv-archive's API, which registers papers, fetches their files at a
priority, serves PDFs and lists the papers of a date window. Knows `core` and nothing else of
litwatch."""

from litwatch.archive.client import (
    BACKGROUND,
    ArchiveClient,
    ArchiveError,
    ArchiveUnavailable,
    ArchiveVersionUnknown,
    listed,
    metadata,
)

__all__ = [
    "BACKGROUND",
    "ArchiveClient",
    "ArchiveError",
    "ArchiveUnavailable",
    "ArchiveVersionUnknown",
    "listed",
    "metadata",
]
