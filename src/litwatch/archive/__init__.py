"""The archive client: arxiv-archive's API, which registers papers, fetches their files at a
priority, serves PDFs and LaTeX text and lists the papers of a date window; and a paper's full
text read from it (`text`). Knows `core` and nothing else of litwatch."""

from litwatch.archive.client import (
    BACKGROUND,
    URGENT,
    WANTED,
    ArchiveClient,
    ArchiveError,
    ArchiveUnavailable,
    ArchiveVersionUnknown,
    PaperText,
    listed,
    metadata,
)
from litwatch.archive.text import (
    PdfText,
    TextUnavailable,
    read_pdf,
    read_text,
    retry_downloads,
    stored_text,
)

__all__ = [
    "BACKGROUND",
    "URGENT",
    "WANTED",
    "ArchiveClient",
    "ArchiveError",
    "ArchiveUnavailable",
    "ArchiveVersionUnknown",
    "PaperText",
    "TextUnavailable",
    "listed",
    "metadata",
    "PdfText",
    "read_pdf",
    "read_text",
    "retry_downloads",
    "stored_text",
]
