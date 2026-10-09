"""The collection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from litwatch import events
from litwatch.core import Paper, rewritten

# What `row_to_paper` reads; the stored body of every match would be megabytes nobody looks at.
COLUMNS = """
    p.id, p.title, p.abstract, p.authors, p.categories, p.primary_category, p.published,
    p.updated, p.url, p.version, p.revision, p.version_history, p.doi, p.journal_ref, p.license
"""


@dataclass
class Upserted:
    inserted: int = 0
    updated: int = 0
    # Ids whose title or abstract was rewritten: what was said about the old text is stale.
    rewritten: list[str] = field(default_factory=list)


def row_to_paper(row: dict[str, Any]) -> Paper:
    return Paper(
        id=row["id"],
        title=row["title"],
        abstract=row["abstract"],
        authors=list(row["authors"] or []),
        categories=list(row["categories"] or []),
        primary_category=row.get("primary_category"),
        published=_iso(row.get("published")),
        updated=_iso(row.get("updated")),
        url=row.get("url"),
        version=row.get("version"),
        revision=row.get("revision") or 1,
        version_history=row.get("version_history") or [],
        doi=row.get("doi"),
        journal_ref=row.get("journal_ref"),
        license=row.get("license"),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def upsert(
    conn: psycopg.AsyncConnection, papers: list[Paper], *, source: str = "arxiv"
) -> Upserted:
    """Insert new papers and refresh metadata on replacements, emitting `paper.added` and
    `paper.revised`.

    A revision counts as rewritten only when its title or abstract changed beyond a touch-up
    (`core.rewritten`); only then is the paper triaged again. Atom metadata can insert a paper
    but never replace canonical metadata.
    """
    result = Upserted()
    async with conn.transaction():
        before = {
            r["id"]: r
            for r in await (
                await conn.execute(
                    "SELECT id, title, abstract, version FROM papers WHERE id = ANY(%s)",
                    ([p.id for p in papers],),
                )
            ).fetchall()
        }
        async with conn.cursor() as cur:
            for p in papers:
                await cur.execute(
                    """
                    INSERT INTO papers (id, title, abstract, authors, categories, primary_category,
                                        published, updated, url, metadata_source,
                                        version, version_history,
                                        doi, journal_ref, license)
                    VALUES (%(id)s, %(title)s, %(abstract)s, %(authors)s, %(categories)s,
                            %(primary_category)s, %(published)s, %(updated)s, %(url)s, %(source)s,
                            %(version)s, %(version_history)s, %(doi)s, %(journal_ref)s, %(license)s)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title, abstract = EXCLUDED.abstract,
                        authors = EXCLUDED.authors, categories = EXCLUDED.categories,
                        primary_category = coalesce(EXCLUDED.primary_category,
                                                    papers.primary_category),
                        published = least(papers.published, EXCLUDED.published),
                        updated = CASE WHEN %(source)s = 'oai'
                                       AND papers.updated::date = EXCLUDED.updated::date
                                       THEN papers.updated ELSE EXCLUDED.updated END,
                        url = EXCLUDED.url, metadata_source = EXCLUDED.metadata_source,
                        version = CASE WHEN EXCLUDED.version IS NOT NULL THEN EXCLUDED.version
                            WHEN EXCLUDED.updated IS NOT NULL AND (papers.updated IS NULL
                                 OR EXCLUDED.updated > papers.updated) THEN NULL
                            ELSE papers.version END,
                        version_history = CASE
                            WHEN EXCLUDED.version_history <> '[]'::jsonb
                                 THEN EXCLUDED.version_history
                            WHEN EXCLUDED.version IS NULL AND EXCLUDED.updated IS NOT NULL
                                 AND (papers.updated IS NULL OR EXCLUDED.updated > papers.updated)
                                 THEN '[]'::jsonb
                            ELSE papers.version_history END,
                        doi = coalesce(EXCLUDED.doi,papers.doi),
                        journal_ref = coalesce(EXCLUDED.journal_ref,papers.journal_ref),
                        license = coalesce(EXCLUDED.license,papers.license)
                    WHERE %(source)s <> 'atom'
                      AND (papers.version IS NULL OR EXCLUDED.version IS NULL
                           OR EXCLUDED.version >= papers.version)
                      AND (%(source)s <> 'oai' OR papers.updated IS NULL
                           OR EXCLUDED.updated::date >= papers.updated::date)
                    RETURNING (xmax = 0) AS inserted, version
                    """,
                    {
                        **p.model_dump(),
                        "authors": Jsonb(p.authors),
                        "version_history": Jsonb(p.version_history),
                        "source": source,
                    },
                )
                row = await cur.fetchone()
                if row is None:
                    continue
                if row["inserted"]:
                    result.inserted += 1
                    await events.emit(conn, events.PAPER_ADDED, p.id, version=row["version"])
                    continue
                result.updated += 1
                old = before[p.id]
                changed = rewritten(old["title"], old["abstract"], p)
                if changed:
                    # What triage said was about other text: the paper is untriaged again.
                    # Its candidates and verdicts stay as history under the old revision.
                    result.rewritten.append(p.id)
                    await cur.execute("DELETE FROM triage WHERE paper_id = %s", (p.id,))
                    await cur.execute(
                        "UPDATE papers SET revision = revision + 1 WHERE id = %s", (p.id,)
                    )
                if changed or row["version"] != old["version"]:
                    await events.emit(
                        conn,
                        events.PAPER_REVISED,
                        p.id,
                        version=row["version"],
                        previous_version=old["version"],
                        rewritten=changed,
                    )
    return result


async def get(conn: psycopg.AsyncConnection, paper_id: str) -> Paper | None:
    row = await (
        await conn.execute(f"SELECT {COLUMNS} FROM papers p WHERE p.id = %s", (paper_id,))
    ).fetchone()
    return row_to_paper(row) if row else None


async def get_many(conn: psycopg.AsyncConnection, ids: list[str]) -> list[Paper]:
    """The stored papers among `ids`, in the order given."""
    rows = await (
        await conn.execute(f"SELECT {COLUMNS} FROM papers p WHERE p.id = ANY(%s)", (ids,))
    ).fetchall()
    by_id = {r["id"]: row_to_paper(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


async def count(conn: psycopg.AsyncConnection) -> int:
    return (await (await conn.execute("SELECT count(*) AS n FROM papers")).fetchone())["n"]
