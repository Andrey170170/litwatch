"""The clear-prose replay's Gists from the abstract and digests (evals/2026-10-19-clear-prose.md).

Run from the repository root against `clear_prose`, a scratch copy of production
(scripts/refresh-sandbox.sh --production --db clear_prose), never another database: to write a
digest again it deletes that digest from the copy, so its papers are due once more, and composes
it without storing. Production's text and the new one go to gists.jsonl and digests.jsonl.

    uv run python evals/2026-10-19-clear-prose/replay.py
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from litwatch import analysis, digests, store
from litwatch import config as configuration
from litwatch.core.digests import DigestPeriod
from litwatch.llm.gateway import build
from litwatch.store import papers as papers_store
from litwatch.store import taxonomy as taxonomy_store

HERE = Path(__file__).parent
# The three opaque Gists read on 2026-10-19, then nine more abstract Gists in md5(paper_id) order.
GISTS_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (g.paper_id) g.paper_id, g.text, a.primary_class, a.weight
    FROM gists g
    JOIN papers p ON p.id = g.paper_id AND g.paper_revision = p.revision
    JOIN analyses a ON a.paper_id = g.paper_id AND a.paper_revision = g.paper_revision
    WHERE g.source = 'abstract'
    ORDER BY g.paper_id, g.at DESC
)
(SELECT * FROM latest WHERE text LIKE ANY (ARRAY['%length-τ observation prefix%',
    'Introduces MFlow%', 'CocoRNA%']))
UNION ALL
(SELECT * FROM latest WHERE NOT text LIKE ANY (ARRAY['%length-τ observation prefix%',
    'Introduces MFlow%', 'CocoRNA%']) ORDER BY md5(paper_id) LIMIT 9)
"""
DIGESTS = [
    ("circuits", DigestPeriod.DAY, date(2026, 10, 16)),
    ("interpretability", DigestPeriod.DAY, date(2026, 10, 15)),
    ("cot-faithfulness", DigestPeriod.WEEK, date(2026, 10, 12)),
    ("alignment-and-safety", DigestPeriod.WEEK, date(2026, 10, 12)),
]


async def main() -> None:
    url = configuration.database_url()
    if not url.endswith("/clear_prose"):
        raise SystemExit("this replay deletes digests: point it at the clear_prose copy only")
    gateway = build(configuration.load(None).models)
    async with await store.connect(url) as conn:
        tree = await taxonomy_store.load(conn)
        rows = await (await conn.execute(GISTS_SQL)).fetchall()
        papers = {p.id: p for p in await papers_store.get_many(conn, [r["paper_id"] for r in rows])}
        with (HERE / "gists.jsonl").open("w", encoding="utf-8") as out:
            for r in rows:
                area = tree.get(r["primary_class"])
                answer = await gateway.ask(
                    analysis.ROLE, analysis.request(papers[r["paper_id"]], area)
                )
                new = answer.value
                line = {
                    "paper_id": r["paper_id"],
                    "title": papers[r["paper_id"]].title,
                    "old": {"gist": r["text"], "weight": r["weight"]},
                    "new": {"gist": new.gist, "weight": new.weight.value},
                }
                out.write(json.dumps(line, ensure_ascii=False) + "\n")
                print("gist", r["paper_id"])

        topics = {t.slug: t for t in digests._active(tree)}
        run = SimpleNamespace(gateway=gateway)
        with (HERE / "digests.jsonl").open("w", encoding="utf-8") as out:
            for slug, period, day in DIGESTS:
                old = await (
                    await conn.execute(
                        "DELETE FROM digests WHERE topic = %s AND period = %s AND day = %s "
                        "RETURNING gist, text",
                        (slug, period.value, day),
                    )
                ).fetchone()
                o = await digests.compose(conn, run, tree, topics[slug], period, day)
                line = {
                    "topic": slug, "period": period.value, "day": day.isoformat(),
                    "old": {"gist": old["gist"], "text": old["text"]},
                    "new": {"gist": o.gist, "text": o.text, "state": o.state.value,
                            "error": o.error},
                }  # fmt: skip
                out.write(json.dumps(line, ensure_ascii=False) + "\n")
                print("digest", slug, period.value, day, o.state.value)


if __name__ == "__main__":
    asyncio.run(main())
