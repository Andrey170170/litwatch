"""Bounded paired primary-model trial; writes evidence only, never production data."""

import argparse
import asyncio
import json
import random
import time
from pathlib import Path

from openai import AsyncOpenAI

from litwatch.config import Settings
from litwatch.evalset import load
from litwatch.models import Paper
from litwatch.screen import Cascade, ModelScreener
from litwatch.selection import Selection


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--budget", type=float, default=2)
    parser.add_argument("--candidate", default="deepseek/deepseek-v4.1-flash")
    parser.add_argument("--confirm-model", default="meta/muse-spark-1.3-contributor")
    parser.add_argument("--group", choices=["all", "random_feed"], default="all")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if args.budget <= 0 or args.candidate == "openai/gpt-5.6-luna":
        parser.error("Budget must be positive and candidate must differ from Luna")
    if not 1 <= args.concurrency <= 8:
        parser.error("Concurrency must be between 1 and 8")
    data = json.loads(args.snapshot.read_text())
    selections = [Selection.model_validate(s) for s in data["selections"]]
    slugs = {s.slug for s in selections}
    cases = {}
    for item in load(Path("evals/labeled.yaml")).papers:
        if (item.note or "").startswith("gold:") or 1 in (item.expected or {}).values():
            cases[item.paper.id] = {
                "paper": item.paper.model_dump(mode="json"),
                "group": "gold" if (item.note or "").startswith("gold:") else "positive_control",
                "expected": {s: t for s, t in (item.expected or {}).items() if s in slugs},
                "note": item.note,
            }
    for row in random.Random(20260910).sample(data["papers"], 32):
        cases.setdefault(
            row["paper"]["id"], {"paper": row["paper"], "group": "random_feed", "expected": {}}
        )
    if args.group != "all":
        cases = {key: case for key, case in cases.items() if case["group"] == args.group}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    manifest = args.output.with_suffix(".manifest.json")
    payload = {
        "selections": data["selections"],
        "cases": list(cases.values()),
        "seed": 20260910,
        "confirm_model": args.confirm_model,
        "candidate": args.candidate,
    }
    if manifest.exists() and json.loads(manifest.read_text()) != payload:
        raise RuntimeError("Frozen trial inputs changed")
    manifest.write_text(json.dumps(payload, indent=2))
    previous = (
        [json.loads(line) for line in args.output.read_text().splitlines()]
        if args.output.exists()
        else []
    )
    if any(r.get("error") for r in previous):
        raise RuntimeError("Inspect previous errors before resuming")
    done = {(r["id"], r["primary"]) for r in previous}
    cost = sum(r["cost"] for r in previous)
    calls_path = args.output.with_suffix(".calls.jsonl")
    calls = (
        [json.loads(line) for line in calls_path.read_text().splitlines()]
        if calls_path.exists()
        else []
    )
    if abs(sum(r["usage"]["cost"] for r in calls) - cost) > 1e-9:
        raise RuntimeError("Unfinished cascade charges: reconcile before resuming")
    client = AsyncOpenAI(
        api_key=Settings.from_env().openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=90,
        max_retries=0,
    )
    lock = asyncio.Lock()
    stopped = False

    class Metered(ModelScreener):
        async def screen(self, paper, selected):
            nonlocal cost, stopped
            # Reserve a conservative allowance before admitting another bounded call.
            async with lock:
                if stopped or cost + 0.15 > args.budget:
                    raise RuntimeError("Budget or accounting stop")
                cost += 0.15
            started = time.monotonic()
            try:
                output, usage = await asyncio.wait_for(super().screen(paper, selected), timeout=90)
                if usage.cost is None:
                    raise RuntimeError("Unknown provider charge")
            except Exception:
                stopped = True
                raise
            async with lock:
                cost += usage.cost - 0.15
                with calls_path.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "id": paper.id,
                                "model": self.model,
                                "selections": [s.slug for s in selected],
                                "usage": usage.model_dump(),
                                "seconds": time.monotonic() - started,
                            }
                        )
                        + "\n"
                    )
            return output, usage

    models = ["openai/gpt-5.6-luna", args.candidate]
    jobs = [
        (case, model)
        for case in cases.values()
        for model in models
        if (case["paper"]["id"], model) not in done
    ]
    print(json.dumps({"cases": len(cases), "jobs": len(jobs), "budget": args.budget}), flush=True)

    async def one(case, model):
        record = {
            "id": case["paper"]["id"],
            "primary": model,
            "group": case["group"],
            "expected": case["expected"],
        }
        started = time.monotonic()
        cascade = Cascade(
            selections,
            Metered(client, model, "low", max_completion_tokens=8192),
            Metered(client, "z-ai/glm-5.3", "high", max_completion_tokens=8192),
            Metered(client, args.confirm_model, "high", max_completion_tokens=8192),
        )
        try:
            result = await cascade.screen(Paper.model_validate(case["paper"]))
            record.update(
                result=result.model_dump(mode="json"), cost=sum(u.cost for u in result.usage)
            )
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        record["seconds"] = time.monotonic() - started
        with args.output.open("a") as stream:
            stream.write(json.dumps(record) + "\n")
        print(
            json.dumps({k: v for k, v in record.items() if k not in {"result", "expected"}}),
            flush=True,
        )

    try:
        for offset in range(0, len(jobs), args.concurrency):
            if stopped:
                break
            await asyncio.gather(*(one(*job) for job in jobs[offset : offset + args.concurrency]))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
