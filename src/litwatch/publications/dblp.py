"""Published versions from DBLP's SPARQL endpoint (spec section 6).

The endpoint is public and needs no key, but dblp asks clients not to overwhelm it: queries go
one at a time, with a pause between them, under a User-Agent naming litwatch. A DOI the authors
gave arXiv names the published record directly. Otherwise matching finds the preprint's CoRR
record by its arXiv DOI (or CoRR volume), then the records outside CoRR sharing one of its
authors and its title, compared without case or punctuation. The title may be the one DBLP
filed with CoRR or the paper's current arXiv title: CoRR keeps the first version's title,
while authors often retitle a later version to match the publication.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Protocol

import httpx

from litwatch.core import Publication

ENDPOINT = "https://sparql.dblp.org/sparql"
USER_AGENT = "litwatch (+https://github.com/Andrey170170/litwatch)"
TIMEOUT = 20.0
PAUSE = 1.0  # seconds between queries
CORR = "https://dblp.org/streams/journals/corr"
REC = "https://dblp.org/rec/"

PREFIX = "PREFIX dblp: <https://dblp.org/rdf/schema#>\n"

# Records with the preprint's title (DBLP's or `%(title)s`, normalized) and one of its
# authors, outside CoRR.
MATCHES = (
    PREFIX
    + """SELECT DISTINCT ?rec ?year WHERE {
  { ?corr dblp:doi <https://doi.org/10.48550/ARXIV.%(upper)s> }
  UNION { ?corr dblp:publishedInJournalVolume "abs/%(id)s" ; dblp:publishedInStream <%(corr)s> }
  ?corr dblp:title ?t ; dblp:authoredBy ?a .
  ?rec dblp:authoredBy ?a ; dblp:title ?title .
  FILTER (?rec != ?corr)
  FILTER NOT EXISTS { ?rec dblp:publishedInStream <%(corr)s> }
  BIND (REPLACE(LCASE(STR(?title)), "[^a-z0-9]", "") AS ?k)
  FILTER (?k = REPLACE(LCASE(STR(?t)), "[^a-z0-9]", "") || ?k = "%(title)s")
  OPTIONAL { ?rec dblp:yearOfPublication ?year }
}"""
)

RECORD = (
    PREFIX
    + """SELECT ?title ?type ?venue ?journal ?volume ?number ?pages ?year ?doi ?url ?book WHERE {
  <%(rec)s> dblp:title ?title .
  OPTIONAL { <%(rec)s> dblp:bibtexType ?type }
  OPTIONAL { <%(rec)s> dblp:publishedIn ?venue }
  OPTIONAL { <%(rec)s> dblp:publishedInJournal ?journal }
  OPTIONAL { <%(rec)s> dblp:publishedInJournalVolume ?volume }
  OPTIONAL { <%(rec)s> dblp:publishedInJournalVolumeIssue ?number }
  OPTIONAL { <%(rec)s> dblp:pagination ?pages }
  OPTIONAL { <%(rec)s> dblp:yearOfPublication ?year }
  OPTIONAL { <%(rec)s> dblp:doi ?doi }
  OPTIONAL { <%(rec)s> dblp:primaryDocumentPage ?url }
  OPTIONAL { <%(rec)s> dblp:publishedAsPartOf ?part . ?part dblp:title ?book }
} LIMIT 1"""
)

AUTHORS = (
    PREFIX
    + """SELECT ?n ?name WHERE {
  <%(rec)s> dblp:hasSignature ?s . ?s dblp:signatureOrdinal ?n ; dblp:signatureDblpName ?name .
} ORDER BY ?n"""
)

BY_DOI = PREFIX + "SELECT ?rec WHERE { ?rec dblp:doi <https://doi.org/%(doi)s> } LIMIT 1"


class SourceError(Exception):
    """The source could not be asked; nothing is known either way."""


class PublicationSource(Protocol):
    async def find(
        self, paper_id: str, title: str | None = None, doi: str | None = None
    ) -> Publication | None:
        """The published record of an arXiv paper, or None when there is none yet. `title`
        and `doi` are the paper's current arXiv metadata, when known."""

    async def record(self, key: str) -> Publication | None:
        """A record by its DBLP key, or None when DBLP has no such record."""

    async def by_doi(self, doi: str) -> Publication | None:
        """A record by its DOI, or None when DBLP has none with it."""


def _safe_id(paper_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z.\-]*/?\d{4,7}(\.\d{4,5})?", paper_id):
        raise ValueError(f"not an arXiv id: {paper_id}")
    return paper_id


def _safe_key(key: str) -> str:
    key = key.removeprefix(REC).removesuffix(".html").removesuffix(".bib").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-.]+)+", key):
        raise ValueError(f"not a DBLP key: {key}")
    return key


def clean_doi(doi: str) -> str:
    """A bare DOI from a DOI, a doi.org link or `doi:...`; ValueError when it is none."""
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi.strip(), flags=re.IGNORECASE)
    if not re.fullmatch(r"10\.\d{4,9}/[^\s<>\"{}|\\^`]+", doi):
        raise ValueError(f"not a DOI: {doi}")
    return doi


def normalized(title: str) -> str:
    """A title as matching compares it: lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", title.lower())


def choose(candidates: list[tuple[str, int | None]]) -> str:
    """Of the records matching a preprint (key, year), the published version to cite: the
    latest, since a poster or workshop version comes before the full paper; the key breaks a
    tie in one year."""
    return max(candidates, key=lambda c: (c[1] or 0, c[0]))[0]


def _value(row: dict[str, Any], name: str) -> str | None:
    return row[name]["value"] if name in row else None


def name(dblp_name: str) -> str:
    """A person's name without DBLP's homonym number ("Wei Wang 0001")."""
    return re.sub(r"\s+\d{4}$", "", dblp_name)


def publication(key: str, row: dict[str, Any], authors: list[str]) -> Publication:
    """A record as SPARQL returned it."""
    kind = (_value(row, "type") or "").rsplit("#", 1)[-1].lower() or "misc"
    year = _value(row, "year")
    doi = _value(row, "doi")
    volume = _value(row, "volume")
    return Publication(
        dblp_key=key,
        title=(_value(row, "title") or "").rstrip(".") or None,
        authors=authors,
        kind=kind,
        venue=_value(row, "venue"),
        container=_value(row, "journal") if kind == "article" else _value(row, "book"),
        volume=volume,
        number=_value(row, "number"),
        pages=_value(row, "pages"),
        year=int(year) if year and year.isdigit() else None,
        doi=doi.removeprefix("https://doi.org/") if doi else None,
        url=_value(row, "url"),
    )


class Dblp:
    """DBLP over SPARQL, one query at a time with a pause between them."""

    def __init__(self, client: httpx.AsyncClient | None = None, pause: float = PAUSE) -> None:
        self._client = client
        self._pause = pause
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _query(self, query: str) -> list[dict[str, Any]]:
        async with self._lock:
            wait = self._last + self._pause - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                client = self._client or httpx.AsyncClient()
                try:
                    response = await client.get(
                        ENDPOINT,
                        params={"query": query},
                        headers={
                            "Accept": "application/sparql-results+json",
                            "User-Agent": USER_AGENT,
                        },
                        timeout=TIMEOUT,
                    )
                finally:
                    if self._client is None:
                        await client.aclose()
                response.raise_for_status()
                return response.json()["results"]["bindings"]
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise SourceError(f"DBLP: {type(exc).__name__}: {exc}") from exc
            finally:
                self._last = time.monotonic()

    async def find(
        self, paper_id: str, title: str | None = None, doi: str | None = None
    ) -> Publication | None:
        if doi and not doi.lower().startswith("10.48550/"):
            try:
                given = await self.by_doi(doi)
            except ValueError:
                given = None
            if given and given.venue != "CoRR":
                return given
        found = await self.candidates(paper_id, title)
        return await self.record(choose(found)) if found else None

    async def candidates(
        self, paper_id: str, title: str | None = None
    ) -> list[tuple[str, int | None]]:
        """Every record outside CoRR that matches the preprint, as (key, year)."""
        rows = await self._query(
            MATCHES
            % {
                "id": _safe_id(paper_id),
                "upper": paper_id.upper(),
                "corr": CORR,
                # Never empty, so it cannot match a record with no letters in its title.
                "title": normalized(title or "") or "-",
            }
        )
        found = {}
        for row in rows:
            year = _value(row, "year")
            found[row["rec"]["value"].removeprefix(REC)] = (
                int(year) if year and year.isdigit() else None
            )
        return sorted(found.items())

    async def record(self, key: str) -> Publication | None:
        key = _safe_key(key)
        rows = await self._query(RECORD % {"rec": REC + key})
        if not rows:
            return None
        signed = await self._query(AUTHORS % {"rec": REC + key})
        return publication(key, rows[0], [name(r["name"]["value"]) for r in signed])

    async def by_doi(self, doi: str) -> Publication | None:
        doi = clean_doi(doi)
        # DOIs are case-insensitive; DBLP stores them as the publisher wrote them.
        for variant in dict.fromkeys((doi, doi.lower(), doi.upper())):
            if rows := await self._query(BY_DOI % {"doi": variant}):
                return await self.record(rows[0]["rec"]["value"])
        return None
