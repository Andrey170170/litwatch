"""Summarize completed paired trials; distinguish labels from feed agreement."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from litwatch.models import ScreenResult, StageRole


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, nargs="+")
    args = parser.parse_args()
    rows = [json.loads(line) for path in args.results for line in path.read_text().splitlines()]
    pairs = defaultdict(dict)
    errors = []
    for row in rows:
        if row.get("error"):
            errors.append({k: row[k] for k in ("id", "primary", "error")})
        else:
            pairs[row["id"]][row["primary"]] = row
    paired = [p for p in pairs.values() if len(p) == 2]
    summary = {"completed_pairs": len(paired), "errors": errors, "groups": {}, "disagreements": []}
    for group in ("gold", "positive_control", "random_feed"):
        totals = {}
        for pair in paired:
            if next(iter(pair.values()))["group"] != group:
                continue
            for model, row in pair.items():
                t = totals.setdefault(
                    model,
                    {
                        "papers": 0,
                        "cost": 0,
                        "seconds": 0,
                        "stage_cost": defaultdict(float),
                        "stage_calls": defaultdict(int),
                        "label_metrics": {
                            r.value: {
                                "exact": 0,
                                "n": 0,
                                "positive_hit": 0,
                                "positive_n": 0,
                                "tier1_hit": 0,
                                "tier1_n": 0,
                            }
                            for r in StageRole
                        },
                    },
                )
                result = ScreenResult.model_validate(row["result"])
                t["papers"] += 1
                t["cost"] += row["cost"]
                t["seconds"] += row["seconds"]
                for stage in result.stages:
                    t["stage_cost"][stage.role] += stage.usage.cost or 0
                    t["stage_calls"][stage.role] += 1
                for role in StageRole:
                    m = t["label_metrics"][role]
                    predicted = result.verdicts_after(role)
                    for slug, expected in row["expected"].items():
                        tier = predicted[slug].tier
                        m["n"] += 1
                        m["exact"] += tier == expected
                        m["positive_n"] += expected <= 2
                        m["positive_hit"] += expected <= 2 and tier <= 2
                        m["tier1_n"] += expected == 1
                        m["tier1_hit"] += expected == 1 and tier == 1
        summary["groups"][group] = totals
    for pair in paired:
        models = list(pair)
        a, b = [ScreenResult.model_validate(pair[m]["result"]) for m in models]
        for slug in a.verdicts:
            av, bv = a.verdicts[slug], b.verdicts[slug]
            if av.tier != bv.tier:
                summary["disagreements"].append(
                    {
                        "id": a.paper_id,
                        "group": pair[models[0]]["group"],
                        "selection": slug,
                        "expected": pair[models[0]]["expected"].get(slug),
                        "verdicts": {
                            models[0]: av.model_dump(mode="json"),
                            models[1]: bv.model_dump(mode="json"),
                        },
                    }
                )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
