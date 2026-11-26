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
| `brief` | chat | Briefs, Weight and Gists from the full text | Sonnet 5.5 at high effort |
| `writer` | chat | digests and their Gists | Opus 5.5 |

The first v1's `triage` and `review` roles are removed (`classify`, `analyse` and `writer` replace them). Deep cards are
made by the `deep` service on its own subscription and are not a gateway role.

Each role that writes (`analyse`, `judge`, `check`, `field_grade`, `brief`, `writer`) names its **fallbacks**: an
ordered list of models, each with its provider, model and effort, asked in turn when the role's model fails for the
paper. A list may be empty: `field_grade`'s is, as no candidate graded Field topics close to Haiku 5.5's own agreement
and Haiku has not failed a Field grading, so one it cannot answer stops ungraded. The lists were chosen by a replay
([`evals/2026-10-19-fallback-ladders.md`](../../../evals/2026-10-19-fallback-ladders.md), the failure-handling plan's
P1) and are the configuration's defaults. The decision roles (`classify`, `area_check`) have none. A call to a fallback
is journalled under the role's name, with the model it went to.

- **A refusal** goes to the next model at once, in the same run; past the last, to the top of the list, for a run that
  started further down. When every model in the list has refused, the step stops for that paper at once and waits for
  the owner ([pipeline.md](pipeline.md), section 9), without three runs of asking the same models again.
- **An unusable answer** (invalid or truncated) counts as one of the step's three failed runs, as before, and the next
  run starts one model further down the list: the first run asks the role's model, the second the next, the third the
  one after. One call per run, as many as without fallbacks. Only unusable answers move it: a full-text pass that found
  no text to read counts a failed run but asks the same model next time.
- **A call that got no answer** (a timeout, a provider error, an `account` error, below) never moves down the list: it
  says nothing about the paper and is not counted against it.
- **`writer`** writes digests, which are retried every run until written rather than stopped after three
  ([digests.md](digests.md), a digest is a record). A refusal goes down the list at once, as above. Each failed digest of
  the topic in a row starts the next attempt one model further down from where the failed one started (a refusal
  past the last model goes on to the top, but the next attempt still starts lower), and the last model in the list is
  kept until a digest of the topic is published. A digest every model refused is failed like any other. An invalid
  answer is the one exception to one call per run: the model that gave it is asked once more, told what was wrong,
  and the digest fails when that ask brings no usable answer either; one that brings no answer at all still leaves
  the first's unusable answer to move the next attempt down ([digests.md](digests.md), writing).

A fallback may come from the same lab as the role's model (refusals follow a lab's policy, but a lab's models differ:
Sonnet 5.5 answered all three papers Opus 5.5 refused,
[`evals/2026-10-16-sonnet-briefs.md`](../../../evals/2026-10-16-sonnet-briefs.md)); each list is written out in order.
An answer any model in the list has given is answered from the journal when the question is asked again, and the role
starts from the model that gave it, so an unchanged question costs nothing after a refusal either.

## The gateway

Carried from the first v1: `Gateway.ask(role, request, *, paper_id)` resolves the role to its list of models (above),
each a provider, model and effort; for each it applies retries and the timeout, journals the call with its cost, and
reuses an identical earlier call (identical includes the resolved provider, model, effort and routing, so `judge` and
`check` never share a reply).
Providers: `anthropic` (prompt caching, cost from a price table), `openrouter`, and `openai` (OpenAI's own API on the
owner's credit: the Responses API with a strict schema, the system prompt cached, cost from a price table; no role is
on it by default). Its explicit cache controls need GPT-5.6 or later, and the price table lists the GPT-6 and GPT-6.x
models only; a normal call asks for the default service tier, and a reply served at any other tier, or by a model
without a listed price, is journalled with its charge unknown; a reply is priced as the model the response names,
and journalled as the model configured. Each send has a journal row of its own, opened once the ceiling admits it and
before it goes, with the model and effort configured: an attempt tried again is settled with its charge (an OpenAI
response that failed carries one; a request the provider rejected, a 4xx, is charged nothing), or none when unknown
(a timeout, a lost connection, a 5xx, which keeps its reservation under a ceiling), and the retry, or the normal call
after a batch that did not answer, gets a new row. A call that sends nothing has none, nor does one given a reply
shared with an identical call. A failure is accounted for, never counted as the provider's reply.

A provider's 401, 402 or 403 is an **`account` error** (a key revoked, credits used up, access withdrawn), in normal
and batched calls alike: never retried, never counted against a paper, and shown on the provider's component of the
status document until its next answered call ([data.md](data.md), configuration). OpenRouter also answers 403 for
the request's content: a moderation flag, a guardrail's block, a filter's `content_policy_violation`, a model's
`refusal` sent as an error. Told by the error's type and metadata, such a 403 is **a refusal** (above), with that
category; a budget used up or a permission denied is the account's, and so is a 403 that can't be told apart, which
shows on the status document rather than passing quietly down the list. OpenAI's 429 for a credit, spend or usage
limit used up is an `account` error too, and a request it declines for its content (an error coded `invalid_prompt`,
`bio_policy` or `misalignment_policy_violation`) a refusal with that category.

An invalid reply's error names where the validator failed it and what it said there, never the reply's text.
Debug mode (`debug = true` in `litwatch.toml`, or `LITWATCH_DEBUG=1`; off by default) is for catching problems over a
stretch of nights: a call whose reply could not be used (invalid, truncated or refused) also keeps the reply's text in
`inference_calls.reply`. Either way only validated outputs are reused.

New: a second request shape for decision models. `Gateway.decide(role, decision_request, *, paper_id)` sends content
plus a list of questions, each a predicate (yes/no) or a choice among given options, and returns per predicate the
probability of yes and per choice the option and its probability. A score question is added when a role needs one.
A role with `decisions = true` is served by OpenRouter's Decisions endpoint (Luna Decisions, Jev; `noul` and `choice`
questions); any other model is asked the same questions as a chat with a strict schema, one property per question. The
endpoint's reply is translated into that schema, so both are validated alike (every question answered, every
probability in [0, 1], every choice one of its options) and journalled, priced and reused like chat calls (Luna
Decisions and Jev bill input tokens only).

## Batches

The nightly run's Anthropic and OpenAI calls go through their batches, at half the price of normal calls; the run
starts at 02:00 and its results are needed by morning, so latency costs nothing. Batching is inside the gateway: a pipeline step
asks for one answer and awaits it as before, and neither requests nor passes say whether they may be batched. **The
job decides**: a nightly run started between the schedule's nightly time and its deadline batches; the app, the
service's workers, the CLI's other commands and a nightly started outside that window use a gateway without batching.

- **Collecting.** A batching gateway holds each call it cannot reuse and sends the held calls as one batch once none
  has joined for 10 seconds, or once they reach 10,000 requests or 200 MB. Identical calls held together share one
  request. A step queues all its calls at once in batch mode, so a step is one batch, not many. Normal calls stay
  bounded: the gateway keeps a limit of normal calls in flight per provider (what the steps' own limits did), so a
  step whose calls are not batched, a deadline's leftovers or a burst of fallbacks never go out all at once.
- **Answers.** Open batches are polled every minute. Every answer of a batch is journalled with its own usage at half
  price, whether or not a call still awaits it, so a batch is accounted for in full; an awaited answer is validated
  and handled as in a normal call (a refusal goes down the role's fallbacks), and only a validated answer is reused. An answer
  no call awaits has its usage on its journal row; the answer itself stays in the batch, which a run started again
  reads when it asks for it (below). A role's timeout bounds each request to the provider, not the wait for a batch. A
  request the batch could not answer (errored, expired, cancelled) is sent again as a normal call when it is asked.
  A batch the provider no longer has (deleted, or the key is another workspace's) never answers: its calls are
  settled as unanswered, and those still asked go as normal calls.
- **Nothing is paid twice once a batch is recorded.** A batched call's journal row is written before the batch is
  sent and given the batch id once the provider accepts it; its request key is its id in the batch. A run that asks a
  key already in an open batch waits on that batch, so a run started again after a crash attaches to its batches
  instead of resubmitting, and takes a refusal or truncation its batch already gave the item's attempt before rather
  than asking the same model again. A run's wait follows its deadline: under the nightly's, until it (cancelled at it,
  then the drain); a nightly started after its deadline has its own start for one, so it cancels at once and drains; a
  call with no deadline (a worker's, a job's) looks once, the batch's next look or lookup, and is sent as a normal call
  if that does not end it (at once while the batch is being created), the batch followed on by whoever owns it, or,
  followed by no one before, settled as at a deadline: cancelled at once, then drained. That one look is the caller's
  limit, not the batch's: a batch keeps its own deadline, its sender's or the latest of the calls waiting on it with
  one. A call in the same process looks at a batch its process follows, a live item's included. A batch is one batch
  from its rows' opening on, whatever it is known by (the reference its provider staged, then its id): one task in the
  process follows it, from before its rows can be read. A call whose batch was sent with
  neither its id nor a reference to find it by recorded (the process died in between) is sent again: that one batch
  may be paid twice.
- **The deadline.** `[schedule] deadline` (07:30 in the schedule's zone) is the nightly's: the first after the
  schedule's nightly time that the run belongs to, kept by a run started again the same night. At the deadline the
  run sends no more batches (calls held but not yet sent go as normal calls), cancels its open batches and waits for them to end (cancelling takes minutes; requests in flight may still be
  answered); the answers are kept, and the cancelled requests are sent as normal calls, as is everything the run asks
  after the deadline. A batch still cancelling 30 minutes after the deadline has its unanswered requests sent as normal
  calls all the same, at the risk of paying twice for some.
- **A run started again.** The run lock means no other nightly runs, so a nightly that takes it takes back the leases
  of the automatic full-text passes a dead run held (pipeline.md, section 6), asks their calls again (which attach to
  the open batches), and waits, before the digests, for the passes queued since the night's first run started. The
  batches a dead process left open are taken over once by the process's one Model gate (architecture.md): as
  `serve` starts, or, for one whose item's lease was still live then (a restart within it), once that lease lapses
  unrenewed. Inside the night's window they are waited for until its deadline, so a nightly run again that night
  shares them; outside it they are settled as at a deadline, cancelled at once and waited for (at most 30
  minutes), their answers kept, and what is asked again is sent as normal calls.
- **The owner's actions during a nightly.** While a nightly holds the run lock, the service's full-text worker leaves
  automatic passes to it and works only the passes the owner asked for ("run it now", "Use the PDF"), with its gateway
  without batching; the nightly does not claim those. A pass the owner's word queues meanwhile (a label that makes a
  paper central) is automatic: the nightly takes it, batched.
- **Providers.** Batching is a provider capability (submit, poll, cancel, results with usage per call). `anthropic`
  and `openai` (the Batch API, also at half price) have it; a role on a provider without it is sent as normal calls in
  any job. An OpenAI batch holds one model's requests, so a step whose calls go to two models is two batches. Its
  input file is uploaded first; past the deadline then, no batch is made and its calls go as normal calls, else its
  calls' rows are given the file's reference (`staged:<file id>`) just before the batch is created. A creation whose
  outcome is unknown (it timed out, lost its connection or failed with a 5xx), and a dead run's rows still on a
  reference, are looked for, a few times within two minutes: found, the batch's id goes on the rows before it is
  polled; not found by then, the rows are settled and the calls go as normal calls, with a warning naming the batch,
  at the risk of paying for both; refused for the account, the calls fail on it and the rows wait for a later run. A
  creation the provider rejected (a 4xx, or a connection that never opened) made no batch: its calls go as normal
  calls at once. An OpenAI batch is found by its input file; a Message Batch carries nothing of ours to find it by,
  so Anthropic's is not looked for: its calls go as normal calls at once, with the warning. A batch that ends without answering some of a dead run's calls (one that
  failed has no answers at all) settles them as unanswered. OpenRouter's calls are not batched: its batch API has no
  Decisions endpoint and reports cost per batch, not per call.

## Lab diversity

Unchanged where one model's error would be final:

- **Focus grading.** `judge` and `check` must come from different labs, fallbacks included: start-up refuses a
  configuration where `judge`'s model and fallbacks share a lab with `check`'s model and fallbacks. The lab is the
  organisation that trained the model, read from the OpenRouter prefix, and `anthropic` or `openai` for their direct
  APIs. When
  every model on one side refuses and the other side answers, the grade stands from one lab, marked `one_lab`
  ([pipeline.md](pipeline.md), section 4).
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
- Re-runs and backfills show their estimated cost (calls by role times the mean cost of the role's model and effort
  over its last 200 calls billed at normal prices; a role with none has no estimate, and the offer says so) before
  they start, and take a hard ceiling they cannot exceed; reaching it stops the job and records where.

Anthropic credit (a monthly grant on the owner's account) pays for the Anthropic roles; whatever is left near the end
of a month is the natural budget for one-off re-grades and, after v1, backfill.

## Findings carried from the 0.14 Anthropic branch

Branch `feat/anthropic-backend` (commit 7911f79) is reference only; its findings are in the first spec (section 5,
commit e6940ea) and `evals/2026-10-08-haiku-primary.md`: neutral request shapes with per-provider adapters; Anthropic
cost needs a price table; prompt caching works for a long shared prompt; Claude counts about 1.7 times Luna's tokens
for the same text; refusals carry a category and are journalled; Muse Spark's contributor endpoint has a long latency
tail (90th percentile 47 s) and about 2% errors, so one slow call must not stop a run.
