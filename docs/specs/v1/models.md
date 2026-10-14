# Models, assurance and cost

## Roles

Pipeline code names roles, never models; moving a role between providers or labs is configuration.

| Role | Kind | Used for | Proposed default |
| --- | --- | --- | --- |
| `classify` | decision | classification ([pipeline.md](pipeline.md), section 2) | chosen in N1: Luna Decisions, Jev or Haiku 5.5 |
| `analyse` | chat | Gist, Weight from the abstract, facets | Haiku 5.5 |
| `judge` | chat | Fit for Focus and Area topics | Opus 5.5 |
| `check` | chat | second-lab Fit for Focus topics and escalated Area disagreements | Muse Spark 1.3 (contributor) |
| `area_check` | decision | "does this Fit hold?" for Area verdicts; a "no" or a low-confidence "yes" escalates | the N1 decision model |
| `field_grade` | chat | Fit for Field topics | Haiku 5.5 |
| `brief` | chat | Briefs and Weight from the full text | Opus 5.5 |
| `writer` | chat | digests and their Gists | Opus 5.5 |

The first v1's `triage` and `review` roles are removed (`classify`, `analyse` and `writer` replace them). Deep cards are
made by the `deep` service on its own subscription and are not a gateway role.

## The gateway

Carried from the first v1: `Gateway.complete(role, request, *, paper_id)` resolves the role to a provider, model and
effort, applies retries and the timeout, journals the call with its cost, and reuses an identical earlier call
(identical includes the resolved provider, model, effort and routing, so `judge` and `check` never share a reply).
Providers: `anthropic` (prompt caching, cost from a price table) and `openrouter`.

New: a second request shape for decision models. `Gateway.decide(role, decision_request, *, paper_id)` sends content
plus a list of questions, each a predicate (yes/no), a choice among given options, or a score, and returns per question
the answer, its probability or distribution, and the provider's confidence. Implementations: OpenRouter's Decisions
endpoint (Luna Decisions, Jev) and, for a chat model under comparison, a chat call with a strict schema that asks for a
probability per answer. Journalled and priced like chat calls (Luna Decisions bills input tokens only).

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
- A monthly budget per provider in configuration. When the month's projected spend exceeds it, the status line turns
  amber. Nightly runs are never downgraded or stopped automatically.
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
