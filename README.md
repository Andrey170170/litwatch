# litwatch

Literature watch for an NLP lab: every new arXiv cs.CL/cs.AI/cs.LG paper is
stored and judged against the lab's *selections*, human-written topic specs
with a tier 1 rubric. The first selection is CoT faithfulness in the BonaFide
sense. Design and rationale live in the homelab-config repo under
`docs/litwatch.md`.

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

## Selections and screening

A **selection** is a topic someone in the lab follows: a name, a subject
paragraph, an optional tier 1 rubric, and keyword regexes for the recall
audit. Selections are authored in the lab's Notion data source and synced
into the `selections` table by n8n (`PUT /selections`); the three shipped
with the package (`src/litwatch/seeds/`) seed that data source and let
`litwatch eval` run without a database. `litwatch selections seed|put|list`
manage them by hand, and `--spec FILE_OR_DIR` on `screen`, `eval`, and
`candidates` screens against YAML specs instead, which is how a rubric is
tried before it is pasted into Notion.

Every paper gets one verdict per active selection, on one scale:

| tier | meaning |
|---|---|
| 1 | must read: central to the subject and meets its tier 1 rubric |
| 2 | recommended: central or close, worth the time; the best a selection without a rubric can give |
| 3 | good to know: same ballpark, a source of ideas |
| 4 | not a member |

A paper's `best_tier` is its best verdict across active selections. Tier 1
gets a Zotero item with the PDF and deep processing, tier 2 a Zotero item,
tier 3 the database and Notion only. A selection's `version` is a hash of
its screener-visible content; verdicts record it, so editing a rubric
re-screens only that selection over the last 30 days (members and keyword
hits), and a new selection screens the whole window.

Three stages, a later verdict replacing the earlier one per selection:

1. `screen`: every paper, every selection in one call, Luna at low effort.
2. `rescreen`: any low-confidence verdict, or a tier 4 verdict on a paper
   that hits the selection's keywords. Sol at low effort. The recall step.
3. `confirm`: every tier 1 or 2 verdict, one call per selection with only
   that selection's rubric in context. Muse Spark at high effort, a different
   model family so its errors are less correlated with the first two.

`litwatch eval` reports precision and recall per selection and tier after
each stage plus tier 1 misses and tier 1+2 recall. `litwatch reconfirm`
applies stage 3 to verdicts that predate it; `litwatch rescreen --selection
SLUG [--days N]` forgets verdicts so the next run judges them again.

## Labeled set

`evals/labeled.yaml` holds arXiv id, title, abstract, and the expected tier
per selection (`expected: {cot-faithfulness: 1, mech-interp: 4}`); a
selection absent from a paper's labels is not scored for it.
arXiv metadata is CC0, so abstracts are committed. Add papers with
`litwatch candidates`, review the draft labels by hand, and append.

`evals/candidates.yaml` is the review queue: papers with draft labels from the
screener, sorted by draft tier. Reviewed entries move to `labeled.yaml`, with
the `draft` note replaced or removed. `litwatch eval` on unreviewed drafts
would only measure the screener against itself.
