# Models, assurance and cost

## Roles

Pipeline code names roles, never models; moving a role between providers or labs is configuration.

| Role | Kind | Used for | Proposed default |
| --- | --- | --- | --- |
| `classify` | decision | classification ([pipeline.md](pipeline.md), section 2) | Jev (`typesafe/jev-1.13`, Decisions endpoint), chosen in N1 |
| `analyse` | chat | Gist, Weight from the abstract, facets | Haiku 5.5 |
| `judge` | chat | Fit for Focus and Area topics | Opus 5.5 |
| `check` | chat | second-lab Fit for Focus topics and escalated Area disagreements | Muse Spark 1.3 (contributor) |
| `area_check` | decision | "does this Fit hold?" for Area verdicts; a "no" or a low-confidence "yes" escalates | the N1 decision model |
| `field_grade` | chat | Fit for Field topics | Haiku 5.5 |
| `brief` | chat | Briefs, Weight and Gists from the full text | Sonnet 5.5 at high effort, its refusals asked of a fallback on OpenRouter (below) |
| `writer` | chat | digests and their Gists | Opus 5.5 |

The first v1's `triage` and `review` roles are removed (`classify`, `analyse` and `writer` replace them). Deep cards are
made by the `deep` service on its own subscription and are not a gateway role.

A role may name a **fallback**, another role asked once, as a normal call, when the role's model refuses. The fallback
must come from another lab (refusals follow a lab's policy, so a second model from the same lab tends to refuse the same
paper); start-up refuses a configuration where it does not. `brief` falls back to a model on OpenRouter, chosen by
the model-spend plan's replay ([`docs/plans/model-spend.md`](../../plans/model-spend.md), D5). A paper the fallback
refuses too fails as any refused call does.

## The gateway

Carried from the first v1: `Gateway.complete(role, request, *, paper_id)` resolves the role to a provider, model and
effort, applies retries and the timeout, journals the call with its cost, and reuses an identical earlier call
(identical includes the resolved provider, model, effort and routing, so `judge` and `check` never share a reply).
Providers: `anthropic` (prompt caching, cost from a price table) and `openrouter`.

Debug mode (`debug = true` in `litwatch.toml`, or `LITWATCH_DEBUG=1`; off by default) is for catching problems over a
stretch of nights. A call whose reply could not be used (invalid, truncated or refused) keeps the reply's text in
`inference_calls.reply`, and an invalid one's error names what the validator said, not only where. Out of debug mode
neither is kept. Either way only validated outputs are reused.

New: a second request shape for decision models. `Gateway.decide(role, decision_request, *, paper_id)` sends content
plus a list of questions, each a predicate (yes/no) or a choice among given options, and returns per predicate the
probability of yes and per choice the option and its probability. A score question is added when a role needs one.
A role with `decisions = true` is served by OpenRouter's Decisions endpoint (Luna Decisions, Jev; `noul` and `choice`
questions); any other model is asked the same questions as a chat with a strict schema, one property per question. The
endpoint's reply is translated into that schema, so both are validated alike (every question answered, every
probability in [0, 1], every choice one of its options) and journalled, priced and reused like chat calls (Luna
Decisions and Jev bill input tokens only).

## Batches

The nightly run's Anthropic calls go through Message Batches, at half the price of normal calls; the run starts at
02:00 and its results are needed by morning, so latency costs nothing. Batching is inside the gateway: a pipeline step
asks for one answer and awaits it as before, and neither requests nor passes say whether they may be batched. **The
job decides**: the nightly run's gateway batches; the app, the service's workers and the CLI's other commands use a
gateway without batching, so the owner's actions are answered at once.

- **Collecting.** A batching gateway holds each call it cannot reuse and sends the held calls as one batch once none
  has joined for 10 seconds, or once they reach 10,000 requests or 200 MB. Identical calls held together share one
  request. A step queues all its calls at once in batch mode: the concurrency limits are lifted, so a step is one
  batch, not many.
- **Answers.** Open batches are polled every minute. Each answer is validated, journalled with its own usage at half
  price, and reused like any other call; a refused, truncated or invalid answer is handled as it would be in a normal
  call, the fallback included. A request the batch could not answer (errored, expired) is sent again as a normal call.
- **No call is paid twice.** A batched call's journal row is written when it is submitted, with its batch id; its
  request key is its id in the batch. A run that asks a key already in an open batch waits on that batch, so a run
  started again after a crash attaches to its batches instead of resubmitting.
- **The deadline.** The nightly run has one, `[schedule] deadline` (07:30 in the schedule's zone): open batches are
  cancelled, the answers already in are kept, and the rest are sent as normal calls, as is everything the run asks
  after it. Other batching jobs have none and wait for their batches, up to the provider's 24 hours.
- **Providers.** Batching is a provider capability (submit, poll, cancel, results with usage per call). `anthropic`
  has it; a role on a provider without it is sent as normal calls in any job. OpenRouter's calls are not batched: its
  batch API has no Decisions endpoint and reports cost per batch, not per call.

## Lab diversity

Unchanged where one model's error would be final:

- **Focus grading.** `judge` and `check` must come from different labs; start-up refuses a configuration where they do
  not. The lab is the organisation that trained the model, read from the OpenRouter prefix, and `anthropic` for the
  direct API.
- **Classification** drops a paper silently if it is wrong, so three things stand behind it for Focus topics: the
  uncertain band, the keyword check, and classification recall on Quality ([feedback.md](feedback.md)).

Area escalation uses `check`, so it inherits the two-lab rule. Field grading, analysis and digests have no second
model.

## Assurance per depth

The grading table is in [pipeline.md](pipeline.md), section 4. The reasoning: errors on a Focus topic are what the
owner pays litwatch to prevent, so two labs grade every member. An Area grade decides the day's picks, which the owner
reads closely, so a strong grader is checked cheaply and escalated on doubt. A Field grade only orders a weekly
overview, where an error costs little.

## Budgets

- Month-to-date spend per provider, from the journal, shown in Settings and on Quality.
- A monthly budget per provider in configuration (by default $100 on Anthropic and $25 on OpenRouter, the owner's
  caps since 2026-10-16). When the month's projected spend exceeds it, the status line turns
  amber. Nightly runs are never downgraded or stopped automatically.
- Deep cards, for observation only. The `deep` service counts each card's tokens, turns and list cost (what the run
  would cost at API prices); litwatch records them with the card's attempt. Settings' Runs and cost shows the month to
  date as a subscription estimate beside the providers, with no budget and no amber, and says how many attempts' usage
  is unknown, so the estimate reads as a lower bound when any is.
- Re-runs and backfills show their estimated cost (calls by role times the role's average cost over the last 30 days)
  before they start, and take a hard ceiling they cannot exceed; reaching it stops the job and records where.

Anthropic credit (a monthly grant on the owner's account) pays for the Anthropic roles; whatever is left near the end
of a month is the natural budget for one-off re-grades and, after v1, backfill.

## Findings carried from the 0.14 Anthropic branch

Branch `feat/anthropic-backend` (commit 7911f79) is reference only; its findings are in the first spec (section 5,
commit e6940ea) and `evals/2026-10-08-haiku-primary.md`: neutral request shapes with per-provider adapters; Anthropic
cost needs a price table; prompt caching works for a long shared prompt; Claude counts about 1.7 times Luna's tokens
for the same text; refusals carry a category and are journalled; Muse Spark's contributor endpoint has a long latency
tail (90th percentile 47 s) and about 2% errors, so one slow call must not stop a run.
