"""The evaluations' JSON-lines files: each record is written whole with its newline, so a last
one without is a write a kill interrupted. Readers skip it (`lines`); a writer cuts it off
first (`mend`), in bytes, so a character cut in half goes too."""

from __future__ import annotations

from pathlib import Path


def lines(path: Path) -> list[str]:
    """The file's complete records, in order: none for a file not there."""
    if not path.exists():
        return []
    data = path.read_bytes()
    return [x for x in data[: data.rfind(b"\n") + 1].decode("utf-8").splitlines() if x]


def mend(path: Path) -> None:
    """Cut an interrupted record off the end of the file, before more is appended."""
    if not path.exists():
        return
    with path.open("rb+") as f:
        data = f.read()
        end = data.rfind(b"\n") + 1
        if end != len(data):
            f.truncate(end)
