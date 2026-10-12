"""BibTeX from arXiv metadata, for any paper, in the collection or only in the archive."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

_SPECIAL = str.maketrans({"&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_", "$": r"\$"})


@dataclass(frozen=True)
class Cited:
    """What an arXiv entry needs; an archive search hit carries the same."""

    id: str
    title: str
    authors: list[str]
    year: int | None
    primary_category: str | None = None


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def key(cited: Cited) -> str:
    """`lastnameYEARfirstword`, the usual hand-made key."""
    last = _ascii(cited.authors[0].split()[-1]).lower() if cited.authors else "anon"
    words = [w for w in re.findall(r"[a-z0-9]+", _ascii(cited.title).lower()) if len(w) > 3]
    return re.sub(r"[^a-z0-9]", "", f"{last}{cited.year or ''}{words[0] if words else ''}")


def arxiv_entry(cited: Cited) -> str:
    fields = {
        "title": "{" + " ".join(cited.title.split()).translate(_SPECIAL) + "}",
        "author": " and ".join(a.translate(_SPECIAL) for a in cited.authors),
        "year": str(cited.year) if cited.year else None,
        "eprint": cited.id,
        "archivePrefix": "arXiv",
        "primaryClass": cited.primary_category,
        "url": f"https://arxiv.org/abs/{cited.id}",
    }
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
    return f"@misc{{{key(cited)},\n{body}\n}}\n"


def entries(cited: Iterable[Cited]) -> str:
    """Entries for several papers, keys made unique with a suffix where they collide."""
    seen: dict[str, int] = {}
    out = []
    for c in cited:
        entry = arxiv_entry(c)
        k = key(c)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:
            entry = entry.replace(f"{{{k},", f"{{{k}{chr(ord('a') + seen[k] - 1)},", 1)
        out.append(entry)
    return "\n".join(out)
