"""Replay frozen primary judgments without writing production data.

Input: JSON {selections: [...], papers: [{paper: {...}, screen: {...}}]}.
Output: append-only JSONL, resumable by paper/group. Charges are bounded per call;
stop before another call when the reported cumulative budget is reached.
"""

import argparse
import asyncio
import json
from pathlib import Path

from openai import AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema

from litwatch.config import Settings
from litwatch.evalset import load
from litwatch.models import Paper, ScreenResult
from litwatch.screen import (
    Cascade,
    ModelScreener,
    output_model,
    parse_output,
    render_system_prompt,
    render_user_prompt,
    usage_of,
)
from litwatch.selection import Selection


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("snapshot", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--labels", type=Path)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--budget", type=float, default=3)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=4)
    args = p.parse_args()
    if args.concurrency < 1 or args.limit < 0 or args.offset < 0 or args.budget <= 0:
        p.error("concurrency and budget must be positive; limit and offset nonnegative")
    data = json.loads(args.snapshot.read_text())
    selections = [Selection.model_validate(s) for s in data["selections"]]
    client = AsyncOpenAI(
        api_key=Settings.from_env().openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=120,
        max_retries=0,
    )
    cascade = Cascade(selections, ModelScreener(client, "unused"))
    jobs = []
    if args.labels:
        cot = [s for s in selections if s.slug == "cot-faithfulness"]
        for item in load(args.labels).papers:
            if item.expected_tier("cot-faithfulness") == 1:
                jobs.append(("control", item.paper, cot))
    for row in data["papers"]:
        paper = Paper.model_validate(row["paper"])
        original = ScreenResult.model_validate(row["screen"])
        selected = cascade.rescreen_selections(paper, original.stages[0].output)
        if selected:
            jobs.append(("replay", paper, selected))
    previous = (
        [json.loads(line) for line in args.output.read_text().splitlines()]
        if args.output.exists()
        else []
    )
    if any(x.get("usage", {}).get("cost") is None for x in previous):
        p.error("previous output has unknown charges; reconcile accounting before resuming")
    done = {(x["group"], x["id"]) for x in previous}
    cost = sum(x.get("usage", {}).get("cost") or 0 for x in previous)
    print(json.dumps({"jobs": len(jobs), "done": len(done), "cost": cost}), flush=True)
    pending = [job for job in jobs[args.offset :] if (job[0], job[1].id) not in done][: args.limit]

    async def evaluate(job):
        group, paper, selected = job
        record = {
            "group": group,
            "id": paper.id,
            "selections": [s.slug for s in selected],
            "model": "z-ai/glm-5.3",
            "effort": "high",
        }
        try:
            schema = output_model(selected)
            completion = await client.chat.completions.create(
                model=record["model"],
                messages=[
                    {"role": "system", "content": render_system_prompt(selected)},
                    {"role": "user", "content": render_user_prompt(paper)},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ScreenOutput",
                        "strict": True,
                        "schema": to_strict_json_schema(schema),
                    },
                },
                max_completion_tokens=8192,
                extra_body={"reasoning": {"effort": "high"}, "usage": {"include": True}},
            )
            record["usage"] = usage_of(completion, record["model"]).model_dump()
            record["provider"] = (completion.model_extra or {}).get("provider")
            record["finish_reason"] = completion.choices[0].finish_reason
            record["output"] = {
                k: v.model_dump(mode="json")
                for k, v in parse_output(
                    schema.model_validate_json(completion.choices[0].message.content or ""),
                    selected,
                ).items()
            }
        except Exception as exc:
            record["error"] = str(exc)
        return record

    while pending and cost + 0.05 * min(args.concurrency, len(pending)) <= args.budget:
        batch, pending = pending[: args.concurrency], pending[args.concurrency :]
        records = await asyncio.gather(*(evaluate(job) for job in batch))
        for record in records:
            with args.output.open("a") as out:
                out.write(json.dumps(record) + "\n")
            cost += record.get("usage", {}).get("cost") or 0
            print(
                json.dumps(
                    {
                        "id": record["id"],
                        "group": record["group"],
                        "cost": round(cost, 6),
                        "error": record.get("error"),
                    }
                ),
                flush=True,
            )
        if any(record.get("usage", {}).get("cost") is None for record in records):
            break  # Unknown charge: do not silently continue past the spending bound.
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
