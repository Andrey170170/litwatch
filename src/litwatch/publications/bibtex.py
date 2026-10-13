"""BibTeX for any paper: from its published record when one is known, from arXiv metadata
otherwise, in the collection or only in the archive."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from litwatch.core import Publication

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


def published_entry(cited: Cited, record: Publication) -> str:
    """An entry from the published record, keyed as the arXiv entry would be so citations
    keep working once a paper is published. A record with only a DOI (set by hand, unknown
    to DBLP) adds it to the arXiv entry."""
    if not record.title:
        entry = arxiv_entry(cited)
        return (
            entry[: -len("\n}\n")] + f",\n  doi = {{{record.doi}}}\n}}\n" if record.doi else entry
        )
    container = (record.container or record.venue or "").rstrip(".")
    fields = {
        "title": "{" + " ".join(record.title.split()).translate(_SPECIAL) + "}",
        "author": " and ".join(a.translate(_SPECIAL) for a in record.authors or cited.authors),
        "journal" if record.kind == "article" else "booktitle": container.translate(_SPECIAL)
        or None,
        "volume": record.volume,
        "number": record.number,
        "pages": record.pages.replace("-", "--") if record.pages else None,
        "year": str(record.year) if record.year else None,
        "doi": record.doi,
        "url": record.url,
        "eprint": cited.id,
        "archivePrefix": "arXiv",
    }
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
    kind = record.kind if record.kind in KINDS else "misc"
    return f"@{kind}{{{key(cited)},\n{body}\n}}\n"


# BibTeX types an entry may take from DBLP; anything else is @misc.
KINDS = {"article", "inproceedings", "incollection", "book", "phdthesis", "mastersthesis"}


def entries(cited: Iterable[Cited], records: dict[str, Publication] | None = None) -> str:
    """Entries for several papers, from their published records where `records` has them,
    keys made unique with a suffix where they collide."""
    records = records or {}
    seen: dict[str, int] = {}
    out = []
    for c in cited:
        entry = published_entry(c, records[c.id]) if c.id in records else arxiv_entry(c)
        k = key(c)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:
            entry = entry.replace(f"{{{k},", f"{{{k}{chr(ord('a') + seen[k] - 1)},", 1)
        out.append(entry)
    return "\n".join(out)
