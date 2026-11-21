"""Bring the smoke test's seeded papers into the collection without a model: each is classified
by hand into one class, as the owner's `PUT /api/settings/classification/{id}` does, and
settled. Runs inside the litwatch container after the day's intake.

    python classify.py PAPERS_JSON CLASS"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from litwatch import classification, config
from litwatch.core import Cause
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store

# The seeding queues no automatic Deep card; the stack's own settles do, by its configuration.
RULES = word.Rules(automatic_deep=False)


async def main(fixture: Path, slug: str) -> None:
    ids = [item["id"] for item in json.loads(fixture.read_text())]
    async with DatabaseGate(config.database_url(), min_size=1, max_size=1) as db:
        async with db.read("taxonomy") as unit:
            tree = await taxonomy_store.load(unit)
        assert tree is not None  # the stack's seed saved one
        for paper_id in ids:
            # Classified by hand and settled together.
            async with word.change(db, paper_id, cause=Cause.OWNER, rules=RULES) as change:
                paper = await papers_store.get(change.unit, paper_id)
                if paper is None or not await classification.by_hand(
                    change.unit, paper, tree, [slug]
                ):
                    raise SystemExit(f"{paper_id}: not stored, or changed meanwhile")
    print(f"classified {len(ids)} papers into {slug}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
