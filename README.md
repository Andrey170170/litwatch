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
selects affected papers over the last 30 days (plus existing members), and
a new selection selects the whole window. Selected papers go through the
full cascade; unchanged exact stage requests can reuse validated results.

Three stages, a later verdict replacing the earlier one per selection:

1. `screen`: every paper, every selection in one call, Luna at low effort.
2. `rescreen`: any low-confidence verdict, or a tier 4 verdict on a paper
   that hits the selection's keywords. GLM 5.3 at high effort rejudges only
   the triggering selections; the broad `benchmark` pattern alone does not
   trigger it. Low-confidence checks remain enabled across all topics.
   `LITWATCH_RESCREEN_IGNORED_KEYWORDS` overrides that comma-separated exclusion
   (empty restores every keyword). The output cap is 8192 tokens, including
   reasoning (`LITWATCH_RESCREEN_MAX_COMPLETION_TOKENS`).
3. `confirm`: every tier 1 or 2 verdict, one call per selection with only
   that selection's rubric in context. Muse Spark at high effort, a different
   model family so its errors are less correlated with the first two.

`litwatch eval` reports precision and recall per selection and tier after
each stage plus tier 1 misses and tier 1+2 recall. `litwatch reconfirm`
applies stage 3 to verdicts that predate it; `litwatch rescreen --selection
SLUG [--days N]` forgets verdicts so the next run judges them again.

### Inference cost and recovery

Every OpenRouter request names its providers (`src/litwatch/routing.py`). Lists belong
to models, not stages, so an overridden model never inherits another model's list. A
listed model is served only by its providers, tried in order with fallbacks between them;
every request also sets `require_parameters`. Luna prefers the half-price `openai/flex` endpoint, then `openai`;
GLM uses Baidu, Reka, then Wafer; Muse uses Meta; Kimi uses Wafer, Moonshot, then
Fireworks. Unlisted models keep OpenRouter's routing. `LITWATCH_PROVIDERS` overrides
lists as `model=a|b;model2=c`, and `model=` hands a model back to OpenRouter. The
[2026-10-01 provider trial](evals/2026-10-01-providers.md) explains the choices.
`LITWATCH_SCREEN_TIMEOUT_SECONDS` defaults to 120 and bounds the primary request
including SDK retries. If that deadline expires, the paper stays pending; a client
timeout cannot trigger server-side fallback. Batch submission remains deferred: Flex
already gives Luna the batch discount without the 24-hour window.

`LITWATCH_SCREEN_CONCURRENCY` (16) bounds papers in flight; a paper's confirm calls run
concurrently, one per selection.

Database-backed `litwatch run` and API runs keep an `inference_calls` journal. Each
response's charge is saved before parsing; each validated screening stage is saved
before the next stage starts. A failed recall/confirmation or later database write
therefore does not require repeating successful stages on retry. Reuse requires the
same paper ID, rendered inputs, ordered selection versions, model, effort, output
schema, token cap and routing settings. Explicit `litwatch rescreen` invalidates the
affected papers' reusable stages atomically with forgetting verdicts, preserving the
billing history. It refuses to run concurrently with an active processing run.

`Usage` and structured inference logs include generation ID, provider, served tier,
cached input tokens, cache-write tokens, reasoning tokens and reported cache discount.
Missing provider fields stay null. Reused stages have `reused=true`, zero new `cost`,
their `original_cost`, and a `source_call_id` linking to the paid response. Their token
counts describe that original response; exclude reused stages when summing new tokens.
The journal contains only new requests, so its token totals need no reuse filtering.

Run cost sums the originating run's journal, including billable responses rejected by
validation and successful stages of failed papers. Digest retries are recorded too;
explicit digest regeneration remains a fresh generation. Standalone screen/eval/digest
commands log usage but do not install a run journal. Historical records are not backfilled.
SDK-internal retries are not individually exposed, and calls interrupted before a usable
response retain an unknown cost; these figures are not a reconciled provider invoice.
There is also an unavoidable window between receiving a response and committing it:
provider-side exactly-once execution is not guaranteed after a process/network failure.

For a run's actual cache share and charge by operation/provider:

```sql
SELECT operation, usage->>'provider' AS provider, usage->>'service_tier' AS tier,
       count(*) AS requests,
       sum((usage->>'cost')::numeric) AS recorded_cost,
       sum((usage->>'cached_tokens')::numeric)
         / nullif(sum((usage->>'prompt_tokens')::numeric), 0) AS cached_input_share,
       count(*) FILTER (WHERE usage->>'cost' IS NULL) AS unknown_cost_requests
FROM inference_calls
WHERE run_id = 123
GROUP BY 1, 2, 3;
```

The schema additions are compatible with the previous application. Reverting the
application preserves the journal but temporarily stops reuse/accounting through it;
do not restore an old database over newly ingested papers.

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

## Daily digest coverage

A digest includes newly analyzed member papers that have not appeared in an earlier
issue of **that digest source** (`general` or a selection slug). Publication date is
not an eligibility rule. Overlapping fetches, rubric edits, tier promotions, and
later reading cards do not make covered papers new again. Tier 1/2 bullets and
count-only tier 2/3 members all consume coverage when their issue is saved.

`digest_batches` freezes each issue's papers, judgments, cards, selection specs,
and analysis cutoff before generation. `digest_coverage` records the first saved
issue per source and paper. A failed source remains pending independently of
successful sources; body, tier counts, and coverage commit together. Analysis from
an interrupted run also carries forward. Concurrent screening waits for digest
generation so its results cannot fall behind the cutoff.

### Revisions and older papers

OAI intake admits every changed record, so a revision or late cross-listing of an
old paper we never stored arrives as a first sighting. `published` is the v1 date
from the raw version history (arXiv's canonical `<created>` can be a later
revision's date). A member first posted more than 7 days before we fetched it is
screened, routed and deep-processed as usual, but labelled in the digest: tier 1
keeps its place with a `vN, first posted …` badge, tier 2 goes to a separate
"Older papers, newly updated" section.

A revision of a paper we already stored keeps its verdicts, unless canonical
metadata rewrote its title or abstract (similarity below 0.8; this also covers a
screened Atom fallback whose canonical text differs), which clears `screened_at`
so the next run screens it again. Intake always harvests raw version history,
with or without archive delivery.

`GET /papers?since=` means "changed since" for the Notion projection: screening,
routing, deep processing, or a change to the shown metadata (title, abstract,
authors, `published`) or a best tier moved by pausing or reactivating a selection, both
stamped in `papers.metadata_changed_at`. A member that drops to
tier 4 leaves that feed; `papers.demoted_at` records it (cleared if the paper is a member
again) and `GET /papers/demoted?since=` lists those papers so the projection can archive
their pages. Coverage records the version
each source covered; a newer version of a tier 1/2 paper gets one dated line under
"Updated since we covered them", once per source and version (`digest_revisions`).

`litwatch digest --run ID` previews without consuming coverage (model calls still
cost money); add `--store` to save. Rewriting an issue uses its original snapshot,
even if the current rubric or paper tier changed. `--days N` is an explicit
retrospective preview and cannot be stored as a daily issue.

Startup seeds historical coverage once from known paper links and unambiguous
full titles in older prose. Legacy `Selection name:` sections seed that selection's
coverage; the complete legacy issue seeds general coverage. Unidentifiable titles
and aggregate-only counts are not guessed. Issues predating snapshots can only be
reconstructed from the remaining current data, so review their previews before
rewriting. This migration does not rewrite historical prose or Notion pages.

Database regressions run in CI and the release gate against PostgreSQL. Locally,
point `TEST_DATABASE_URL` at a disposable database and run `uv run pytest`; the tests
create and remove isolated schemas there. Never point that variable at production.

## Shared archive

Daily intake also registers every cs.CL/cs.AI/cs.LG paper with the shared arxiv-archive
service, independent of screening tier; curated routing reuses its PDFs. Deep processing
reads a paper's arXiv HTML when there is one and falls back to the archived PDF. See [the 0.9.0 integration note](docs/ARCHIVE-STAGE2.md) for states, settings and
rollback. Historical snapshot import and backfill belong to arxiv-archive.
