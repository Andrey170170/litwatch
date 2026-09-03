# litwatch

Literature watch for chain-of-thought monitoring and faithfulness papers,
with priority on methods that use model internals. Design and rationale live
in the homelab-config repo under `docs/litwatch.md`.

Phase 1 scope: the screener and its evaluation set. Nothing here fetches on a
schedule or writes to Zotero or Notion yet.

## Setup

```sh
uv sync --extra dev
export OPENROUTER_API_KEY=...   # or put it in .env
```

## Commands

```sh
litwatch screen 2305.04388                # screen one arXiv paper, print the result
litwatch eval                             # run the labeled set, report per-tier precision/recall
litwatch eval --no-rescreen               # first-stage model only
litwatch candidates "abs:faithful AND abs:\"chain of thought\"" --max 30 --out evals/candidates.yaml
litwatch candidates ... --prelabel        # same, with screener output filled in as a draft label
```

## Screening

One structured call per paper on title plus abstract. Output flags:

| flag | meaning |
|---|---|
| `is_cot_monitoring_or_faithfulness` | the paper is about CoT faithfulness, CoT monitoring, hidden or encoded reasoning, or methods that make CoT more faithful or monitorable |
| `uses_internals` | the method or analysis relies on activations, attention, probes, SAEs, patching, steering, weights, or gradients |
| `is_mech_interp` | the paper is mechanistic interpretability, whatever the topic |
| `proposes_definition_or_benchmark` | a faithfulness or monitorability definition, ground-truth construction, benchmark, or metric meta-evaluation |

Tier is derived from the flags: `must_read` (CoT, and either internals or a
definition/benchmark), `aware` (other CoT papers), `mech_interp` (internals
work off topic), `none`. Every model call records tokens and cost as reported
by OpenRouter; `litwatch eval` prints the total per model.

Three stages, each later verdict replacing the earlier one:

1. `screen`: every paper, Luna at low effort.
2. `rescreen`: low-confidence results and negatives that hit the keyword audit
   list, Sol at low effort. The recall step.
3. `confirm`: anything now in `must_read` or `aware`, Muse Spark at high
   effort. The precision step, on a different model family so its errors
   are less correlated with the first two. About 1% of the feed.

`litwatch eval` reports precision and recall after each stage. `litwatch
reconfirm` applies stage 3 to rows screened before it existed. Set
`LITWATCH_CONFIRM_MODEL=meta/muse-spark-1.3-contributor` once the OpenRouter
account has training opt-in enabled; the inputs are public abstracts.

## Labeled set

`evals/labeled.yaml` holds arXiv id, title, abstract, and the expected flags.
arXiv metadata is CC0, so abstracts are committed. Add papers with
`litwatch candidates`, review the draft labels by hand, and append.

`evals/candidates.yaml` is the review queue: papers with draft labels from the
screener, sorted by draft tier. Reviewed entries move to `labeled.yaml`, with
the `draft` note replaced or removed. `litwatch eval` on unreviewed drafts
would only measure the screener against itself.
