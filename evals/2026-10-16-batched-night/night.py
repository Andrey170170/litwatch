"""A nightly run through batches at any hour, for the model-spend plan's P3 night
(docs/plans/model-spend.md): `nightly.run` with the night given rather than read from the
schedule. Run it again with the same --start and --deadline to start the night again.

    uv run python evals/2026-10-16-batched-night/night.py --start ISO --deadline ISO [--limit N]
"""

import argparse
import asyncio
import json
import logging
import os
from datetime import date, datetime

from litwatch import config as configuration
from litwatch import store
from litwatch.jobs import nightly
from litwatch.store import runs


async def main(args: argparse.Namespace) -> None:
    cfg = configuration.load(None)
    night = (datetime.fromisoformat(args.start), datetime.fromisoformat(args.deadline))
    async with await store.connect(os.environ["LITWATCH_DATABASE_URL"]) as conn:
        run_id = await nightly.run(
            conn,
            cfg,
            fetch=False,
            limit=args.limit,
            night=night,
            today=date.fromisoformat(args.today),
        )
        run = await runs.get(conn, run_id)
    print(
        json.dumps({"run": run_id, "cost": float(run["cost"]), "counts": run["counts"]}, indent=1)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--deadline", required=True)
    parser.add_argument("--today", required=True)
    parser.add_argument("--limit", type=int)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(main(parser.parse_args()))
