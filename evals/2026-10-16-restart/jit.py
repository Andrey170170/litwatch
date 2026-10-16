"""PostgreSQL's JIT on the reading API's lists, timed in pairs (N6; the build plan's N6 entry).

    uv run python evals/2026-10-16-restart/jit.py [--repeats 5] [--out DIR]

Against $LITWATCH_DATABASE_URL (the restart's database). The statements are the ones the API
issues: the store's list functions run through a connection that records each statement and its
parameters, for Papers (the whole collection, and each depth) and every shown topic's page.
Each statement is then run under `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` with `jit` on and off
back to back, the order alternating between repeats so neither setting always runs on the warmer
cache. Prints, per list, the median execution time of its statements summed, with JIT on and off,
the median JIT time and how many of its statements JIT compiled; writes every plan to
`DIR/plans.jsonl.gz`.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import statistics
from pathlib import Path
from typing import Any

from litwatch import config as configuration
from litwatch import store
from litwatch.core import Depth
from litwatch.store import reading
from litwatch.store import taxonomy as taxonomy_store


class Recorder:
    """A connection that keeps every statement executed through it, with its parameters."""

    def __init__(self, conn: Any) -> None:
        self.conn, self.seen = conn, []

    async def execute(self, query: Any, params: Any = None, **kw: Any) -> Any:
        self.seen.append((query, params))
        return await self.conn.execute(query, params, **kw)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.conn, name)


async def statements(conn: Any) -> dict[str, list[tuple[str, Any]]]:
    """Each list's statements, as the API's default request for it issues them."""
    tree = await taxonomy_store.load(conn)
    if tree is None:
        raise SystemExit("no taxonomy in this database")
    lists: dict[str, list[tuple[str, Any]]] = {}
    cases: list[tuple[str, Any]] = [("papers", reading.Query())]
    cases += [(f"papers depth={d.value}", reading.Query(depth=d)) for d in Depth]
    for name, query in cases:
        rec = Recorder(conn)
        await reading.papers(rec, tree, query)
        lists[name] = rec.seen
    for t in tree.shown():
        rec = Recorder(conn)
        await reading.topic_papers(rec, tree, t.slug, reading.Query())
        lists[f"topic {t.slug} ({t.depth.value})"] = rec.seen
    return lists


async def explain(conn: Any, query: str, params: Any, jit: bool) -> dict[str, Any]:
    await conn.execute(f"SET jit = {'on' if jit else 'off'}")
    row = await (
        await conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params)
    ).fetchone()
    return next(iter(row.values()))[0]


async def main(repeats: int, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    async with await store.connect(configuration.database_url()) as conn:
        lists = await statements(conn)
        print("| List | Statements | JIT on, ms | JIT off, ms | JIT time, ms | Compiled |")
        print("| --- | --- | --- | --- | --- | --- |")
        with gzip.open(out / "plans.jsonl.gz", "wt") as plans:
            for name, stmts in lists.items():
                on, off, jit_ms, compiled = [], [], [], 0
                for rep in range(repeats):
                    times = {True: 0.0, False: 0.0}
                    spent = 0.0
                    for jit in (True, False) if rep % 2 == 0 else (False, True):
                        for i, (query, params) in enumerate(stmts):
                            plan = await explain(conn, query, params, jit)
                            times[jit] += plan["Execution Time"]
                            if jit and "JIT" in plan:
                                spent += plan["JIT"]["Timing"]["Total"]
                                compiled += rep == 0
                            row = {"list": name, "statement": i, "repeat": rep, "jit": jit}
                            plans.write(json.dumps(row | {"plan": plan}) + "\n")
                    on.append(times[True])
                    off.append(times[False])
                    jit_ms.append(spent)
                print(
                    f"| {name} | {len(stmts)} | {statistics.median(on):.1f} | "
                    f"{statistics.median(off):.1f} | {statistics.median(jit_ms):.1f} | "
                    f"{compiled} |"
                )
        await conn.execute("RESET jit")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    asyncio.run(main(args.repeats, args.out))
