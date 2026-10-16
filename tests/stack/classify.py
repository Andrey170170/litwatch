"""Bring the smoke test's seeded papers into the collection without a model: each is classified
by hand into one class, as the owner's `PUT /api/settings/classification/{id}` does, and
settled. Runs inside the litwatch container after the day's intake.

    python classify.py PAPERS_JSON CLASS"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from litwatch import classification, config, store
from litwatch.feedback import word
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store


async def main(fixture: Path, slug: str) -> None:
    ids = [item["id"] for item in json.loads(fixture.read_text())]
    async with await store.connect(config.database_url()) as conn:
        tree = await taxonomy_store.load(conn)
        for paper_id in ids:
            async with word.change(conn, paper_id):  # classified and settled together
                paper = await papers_store.get(conn, paper_id)
                if paper is None or not await classification.by_hand(conn, paper, tree, [slug]):
                    raise SystemExit(f"{paper_id}: not stored, or changed meanwhile")
    print(f"classified {len(ids)} papers into {slug}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
