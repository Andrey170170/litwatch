"""Starting material for the evaluation, from what 0.x left behind.

Only the owner's own labels are ground truth. Everything else here is model output: it can
show a regression, and it contains none of the papers 0.x itself missed.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import yaml

from litwatch.core import Grade, Label, LabelOrigin, LabelValue, Paper, Status, TopicKind
from litwatch.store import feedback as feedback_store
from litwatch.store import legacy
from litwatch.store import papers as papers_store
from litwatch.store import topics as topics_store

GOLD_MARK = "gold: Andrei"
TIER_GRADES = {1: Grade.MUST_READ, 2: Grade.RELEVANT, 3: Grade.ADJACENT, 4: Grade.NONE}


async def _watched(conn: psycopg.AsyncConnection) -> list[str]:
    topics = await topics_store.all(conn, Status.ACTIVE)
    return [t.slug for t in topics if t.kind == TopicKind.WATCH]


async def _store_missing(conn: psycopg.AsyncConnection, papers: list[Paper]) -> None:
    """Insert papers the collection lacks; a stored paper's metadata is never replaced."""
    have = {p.id for p in await papers_store.get_many(conn, [p.id for p in papers])}
    await papers_store.upsert(conn, [p for p in papers if p.id not in have], source="evalset")


async def from_label_file(conn: psycopg.AsyncConnection, path: Path) -> dict[str, int]:
    """Read the 0.x label file. Entries marked `gold: Andrei` become labels; the rest is
    silver (model agreement, or a hand pass still awaiting him) and joins the regression set.

    A gold tier of 1 or 2 is a label of relevant with that grade; 3 or 4 is not relevant.
    """
    watched = await _watched(conn)
    counts = {"gold": 0, "silver": 0}
    entries = (yaml.safe_load(path.read_text()) or {}).get("papers", [])
    papers = []
    for entry in entries:
        fields = {k: v for k, v in entry.items() if k in Paper.model_fields}
        papers.append(Paper(**fields))
    await _store_missing(conn, papers)
    for entry in entries:
        gold = (entry.get("note") or "").startswith(GOLD_MARK)
        for slug, tier in (entry.get("expected") or {}).items():
            if slug not in watched:
                continue
            if not gold:
                await feedback_store.add_regression(conn, entry["id"], slug, tier, "silver")
                counts["silver"] += 1
                continue
            relevant = tier <= 2
            await feedback_store.set_label(
                conn,
                Label(
                    paper_id=entry["id"],
                    topic=slug,
                    value=LabelValue.RELEVANT if relevant else LabelValue.NOT_RELEVANT,
                    grade=TIER_GRADES[tier] if relevant else None,
                    origin=LabelOrigin.GOLD,
                    note=f"0.x label file, tier {tier}",
                ),
            )
            counts["gold"] += 1
    return counts


async def from_legacy_verdicts(conn: psycopg.AsyncConnection) -> dict[str, int]:
    """Read 0.x's verdicts on the Watch topics from an adopted database.

    Tier 1 and 2 are accepted in bulk as provisional positives: the owner broadly agrees
    with them and corrects any he notices. Tier 1 to 3 join the regression set.

    For M2's evaluation database. Production goes through `db migrate-0x` instead, which
    keeps the tiers as migrated verdicts: refused there, where the provisional labels would
    stand as the owner's word over every later verdict (`jobs.migrate`).
    """
    if not await legacy.adopted(conn):
        raise RuntimeError("this database has no 0.x verdicts; it was not adopted from 0.x")
    found = await (
        await conn.execute("SELECT EXISTS (SELECT 1 FROM verdicts WHERE migrated) AS m")
    ).fetchone()
    if found["m"]:
        raise RuntimeError("this database was migrated with `db migrate-0x`; nothing to seed")
    counts = {"provisional": 0, "regression": 0}
    for row in await legacy.verdicts(conn, await _watched(conn), 3):
        await feedback_store.add_regression(
            conn, row["paper_id"], row["topic"], row["tier"], "legacy"
        )
        counts["regression"] += 1
        if row["tier"] > 2:
            continue
        added = await feedback_store.add_provisional(
            conn,
            Label(
                paper_id=row["paper_id"],
                topic=row["topic"],
                value=LabelValue.RELEVANT,
                grade=TIER_GRADES[row["tier"]],
                origin=LabelOrigin.LEGACY_POSITIVE,
                note=f"0.14 tier {row['tier']}: {row['reason']}",
            ),
        )
        counts["provisional"] += int(added)
    return counts
