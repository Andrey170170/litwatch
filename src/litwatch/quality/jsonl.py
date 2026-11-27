"""The evaluations' JSON-lines files: each record is written whole with its newline, so a last
one without is a write a kill interrupted. Readers skip it (`lines`); a writer cuts it off
first (`mend`), in bytes, so a character cut in half goes too. Records are split on the LF
byte alone: JSON escapes it inside a string, but not U+2028 and the other line breaks that
`str.splitlines` would split a record on."""

from __future__ import annotations

from pathlib import Path


def records(data: bytes, *, whole: bool = False) -> list[str]:
    """The complete records of a JSON-lines file's bytes, in order; with `whole` (a file
    written at once, a packed one), the last too, newline or not."""
    kept = data if whole else data[: data.rfind(b"\n") + 1]
    return [x.decode("utf-8") for x in kept.split(b"\n") if x]


def lines(path: Path) -> list[str]:
    """The file's complete records, in order: none for a file not there."""
    return records(path.read_bytes()) if path.exists() else []


def mend(path: Path) -> None:
    """Cut an interrupted record off the end of the file, before more is appended."""
    if not path.exists():
        return
    with path.open("rb+") as f:
        data = f.read()
        end = data.rfind(b"\n") + 1
        if end != len(data):
            f.truncate(end)
