"""Give one of the smoke test's papers a Deep card in the format from before Deep reads, stored as
the deep service once returned them, so the walk still sees a page that holds one: the service
writes none any more (its contract takes only a Deep read). Its "Among the collection" rows name
the other seeded papers. Runs inside the litwatch container after they are classified.

    python legacy_card.py PAPERS_JSON INDEX"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from litwatch import config
from litwatch.core import Cause
from litwatch.feedback import word
from litwatch.gates.database import DatabaseGate
from litwatch.store import integrations as cards

# As classify.py's: the seeding queues no automatic Deep card.
RULES = word.Rules(automatic_deep=False)


def card(paper: dict, others: list[dict]) -> dict:
    """A Deep card, the format from before, which extends the other seeded papers."""
    return {
        "summary": "A stand-in card seeded by the stack smoke test. " * 8,
        "key_claim": "The smoke test seeded this card; no model read the paper.",
        "paper_kind": "method",
        "evaluation_setup": {
            "models": [],
            "tasks": paper["title"].split()[:2],
            "generalization": "Not judged: no model wrote this card.",
        },
        "prior_work": [
            {"title": "The paper's own references", "relation": "Not read by the smoke test."}
        ],
        "among_collection": [
            {"arxiv_id": o["id"], "title": o["title"], "relation": "extends"} for o in others[:3]
        ],
        "code_available": False,
        "weights_available": False,
        "limitations": ["No model read the paper; this card only shows the wiring."],
    }


async def main(fixture: Path, index: int) -> None:
    items = json.loads(fixture.read_text())
    paper, others = items[index], [p for i, p in enumerate(items) if i != index]
    # Written as the deep job writes a card: in a change, which settles the paper.
    async with (
        DatabaseGate(config.database_url(), min_size=1, max_size=1) as db,
        word.change(
            db, paper["id"], cause=Cause.VERDICT, rules=RULES, name="seed a legacy Deep card"
        ) as change,
    ):
        await cards.put_card(
            change.unit,
            paper["id"],
            card(paper, others),
            "smoke-test",
            {},
            revision=1,
            version=None,
            source="latex",
            pass_id=None,
        )
    print(f"seeded a Deep card from before on {paper['id']}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), int(sys.argv[2])))
