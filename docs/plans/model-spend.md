# Model spend under the caps

Approved by the owner on 2026-10-16. This is the reference for cutting what v1's model calls cost: the order of work,
the decisions and how each part is shown to work. What litwatch does is the spec, [`docs/specs/v1/`](../specs/v1/README.md);
this plan amended [models.md](../specs/v1/models.md) (roles, batches, the fallback),
[pipeline.md](../specs/v1/pipeline.md) (sections 6 and 11), [digests.md](../specs/v1/digests.md) (writing and timing)
and [data.md](../specs/v1/data.md) (configuration). Where this plan and the spec differ, the spec wins and this plan is
corrected. Terms are the glossary's.

## Goal and approach

The owner's caps are about **$100 a month on the Anthropic API** and **$20–30 on OpenRouter**. At the restart's rates
([evals/2026-10-16-restart.md](../../evals/2026-10-16-restart.md): $17.06 per 1,000 papers, about 12,700 papers a month)
v1 costs about $215 a month on Anthropic and $4 on OpenRouter. 80 % of the Anthropic spend is the full-text passes on
Opus 5.5 ($0.208 a pass, about 48k input tokens each; the Area Weight re-checks alone about $134 a month).

The approach, in two PRs:

1. **PR 1 (P2):** the `brief` role moves to Sonnet 5.5, with a model on OpenRouter for its refusals; full-text input
   loses its bibliography; a new revision whose text is unchanged reuses its pass; budgets and the 02:00 nightly.
   Projected: about $120–145 a month.
2. **PR 2 (P3):** every Anthropic call of the nightly run goes through Message Batches at half price, collected inside
   the gateway so the pipeline barely changes, with a 07:30 deadline that finishes what is left as normal calls.
   Projected: about $60–90 a month, about $75 with `brief` at high effort.

Results are needed by 09:00 (the owner wakes then); the nightly starts at 02:00, both in America/Denver.

## Choices made without asking

- **Nightly at 02:00** (`[schedule] nightly = "0 2 * * *"`) in the owner's zone: the deploy configuration sets
  `[schedule] timezone = "America/Denver"`, which follows daylight saving.
- **The nightly's deadline is 07:30**, `[schedule] deadline = "07:30"`, a time of day in the schedule's zone, the first
  one after the run starts. At the deadline open batches are cancelled, answers already in are kept, and the rest go
  as normal calls at full price. The digests' bounded wait ends at the deadline rather than after `wait_minutes`. Jobs
  other than the nightly have no deadline and wait up to a batch's 24 hours.
- **A batched call is journalled like any other call, at half its price.** The price table gets a batch factor of 0.5;
  Anthropic reports usage per request in a batch, so every call keeps its own cost.
- **Restarts resubmit nothing.** A batched call's journal row stores its batch id, and the request key the gateway
  already computes (sha256, 64 hex characters) is the request's `custom_id`. A process that asks a key sitting in an
  open batch waits on that batch.
- **Batch mode lifts the concurrency limits** (`concurrency.bounded`'s limit, the full-text worker's claim of 4), so a
  step's whole set goes in one batch. A pass's lease is extended to cover the deadline.
- **A batch goes out** once no new call has joined it for 10 s, or once it holds 10,000 requests or 200 MB
  (Anthropic's limits are 100,000 and 256 MB; a pass's request is up to about 200 kB). Open batches are polled every
  60 s. Identical calls in one collection window share one request.
- **Batch or normal call is decided by the job, never by the caller.** Neither requests nor passes carry a flag. The
  nightly run's gateway batches. The app, the service's full-text worker and the CLI's other commands use a gateway
  without batching, so the owner's actions ("run it now", Use the PDF, a label that queues a pass) go at once. While a
  nightly run holds the run lock, the service's full-text worker sits out: passes queued or awaiting text then are the
  nightly's, and it works them through its batches.
- **Batching is a provider capability behind one interface** (submit, poll, cancel, results with usage per call).
  Anthropic implements it now. A role whose provider has none is sent as normal calls; OpenRouter's Batch API
  (`POST /api/v1/batches`, live since 2026-09-22) can be plugged in later, once it reports cost per call.
- **The whole `brief` role runs on Sonnet 5.5**, Focus Briefs and Area re-checks alike, at the effort D6 settles. A
  refusal is asked once more of the role's fallback, a model on OpenRouter from another lab (D5), as a normal call.
- **Text trimming.** The bibliography is cut from LaTeX text (the `thebibliography` block the archive inlines). From
  PDF text, the last "References" heading is cut up to the next heading or the end. The appendix is cut only if P1
  shows the Briefs no worse without it.
- **An unchanged text makes no call.** When a pass reads text whose hash (of the text as read, before trimming)
  matches that of the paper's last finished pass that wrote a Brief, that pass's Brief, Gist and Weight are recorded
  for the new pass, at no cost.
- **Budgets** are `anthropic_monthly = 100` and `openrouter_monthly = 25`, as the configuration's defaults.
- **Unchanged:** `judge` stays on Opus 5.5 and the Haiku roles keep their models. Classification, the Area check, the
  second-lab check and the fallback stay normal calls on OpenRouter. Deep cards stay on the `deep` service's
  subscription.

### Where batching sits

```
pipeline step: gateway.ask(role, request)        unchanged: awaits one answer
  -> gateway: reuse from the journal? key already in an open batch?
  -> collector (nightly gateway only): 10 s quiet, 10,000 requests or 200 MB -> submit
  -> poller: every 60 s; at the deadline cancel, keep what is in, send the rest as normal calls
  -> validate, journal (half price), answer the awaiting ask
```

A chain like `judge` then `area_check` then `check` becomes a batch round followed by normal calls; the pipeline code
does not know.

## Decisions

- **D1. Batching lives inside the gateway.** `ask()` waits on a batch the gateway collects; classification, analysis,
  grading, the passes and the digests keep their code, and only the concurrency limits change. Explicit batch steps
  were the alternative: easier to see, but every step rewritten and grading's chain hand-written as rounds. The
  owner's note: the requesting side should not know how a request is made, neither granting batching nor marking a
  request urgent; which job runs decides.
- **D2. No OpenRouter batching now.** OpenRouter's Batch API has no Decisions endpoint (classification and the Area
  check use it), reports cost per batch, not per call, and documents no cancel; what is left (`check`, the fallback)
  is about $1 a month. The interface is shaped so it can be added; some providers have per-model batch endpoints or a
  flex tier instead, which would be good to handle but is not required.
- **D3. The writer is batched, topics in parallel.** Topics do not depend on each other; a topic's own order (its
  failed digests retried, the day's digest, Monday's weekly) is kept. Each topic's chain runs concurrently, so a night
  is 1–3 batch rounds instead of 5–14. About $7 a month saved.
- **D4. Two PRs.** PR 1 is small, lands the first half of the savings at once, and its fallback is a normal call;
  PR 2, batching, is a hard change on its own.
- **D5. The fallback is chosen in P1 between Gemini 4 Argon and GPT-6.1 Sol**, with MiMo-V2.6-Pro third. By
  Artificial Analysis's Intelligence Index (Opus 5.5 at medium, today's `brief`, scores 51): Argon 53 at high, Sol 50
  at high, MiMo-V2.6-Pro 46. Argon and Sol cost $2/$10 per million tokens (about $0.13 a pass), MiMo $0.435/$0.87.
  Price barely matters at a few calls a week; quality and not refusing the same papers do. P1 asks both of every
  refusal; the first that answers them all wins, a tie going to the higher index. If both refuse some, MiMo is tried.
- **D6. Sonnet runs `brief` at high effort; P1 checks medium too.** Sonnet 5.5 scores 41 at medium, 47 at high and 52
  at xhigh. A Brief is careful reading and a Weight judgement, which may need less than the index measures. If medium
  agrees with Opus as well as high does on P1's papers, medium is taken (about $60 a month against $75).

## Risks

- **R1. Sonnet refuses more of the feed, or so does the fallback.** Sonnet declines in `frontier_llm` and
  `general_harms` as well as `cyber` and `bio`, and the feed is mostly papers about frontier LLMs. P1 measures it on
  N6's papers, including the three Opus refused; the fallback candidates are asked of every refusal. A paper all of
  them refuse stays failed, listed on Quality as today.
- **R2. A batch runs long.** Most finish within an hour; 24 hours is allowed. The deadline cancels what is left and
  sends it as normal calls: at worst a night's remaining passes cost full price (about $3), and digests still land
  before 09:00.
- **R3. The process dies mid-night.** Batch ids are persisted, so the next run attaches to open batches instead of
  paying again. If it dies late, the morning has no digests until a run is started by hand; resuming on start-up is out
  of scope.
- **R4. A step's calls trickle out over several batches.** Concurrency is lifted so a step queues all its calls at
  once; P3's night counts rounds and times. Passes whose text arrives late form one more batch, overlapping the rest.
- **R5. Cutting the appendix drops results only it has.** Only the bibliography is cut unless P1's side-by-side shows
  no Brief losing a number or a limit.
- **R6. Batched costs land in the journal wrong**, so budgets read wrong. A test prices a fake batch result; P3's night
  is checked against Anthropic's usage for the same window.
- **R7. The nightly holds the run lock for hours, not under one.** A job started by hand at night (`litwatch digests
  write`, a CLI re-run) waits for it. The app's actions do not take the run lock and still go at once.

## Stages

All work is in a worktree off `v1`; each PR follows [`docs/agents/workflow.md`](../agents/workflow.md).

| Stage | What | Shows before the next starts |
| --- | --- | --- |
| P1 | **Sonnet replay.** 40 of N6's passes (the 3 refused, 12 Focus Briefs, 25 re-checks) replayed on the `n6_restart` database as an evaluation (evaluation tables only). Sonnet at high and at medium; three text variants at high (as now, bibliography cut, appendix also cut). The fallback candidates on every refusal. About $8–10; the owner confirms before it passes $5. | A report in `evals/`: refusals by category; agreement with Opus on Weight and on the overstatement line, per effort; output tokens and cost per effort; tokens per text variant; each fallback candidate's answers. About 8 Opus/Sonnet pairs for the owner to read. **The owner's read decides Sonnet.** |
| P2 | **PR 1.** `brief` on Sonnet with the fallback, trimming as P1 settled it, the hash reuse, budgets, 02:00 and the deadline setting's zone. | Checks green. A full-text pass end to end on a scratch database. A refusal reaches the fallback (shown with a fake). The pass's cost lower in the journal. |
| P3 | **PR 2.** The batch path in the gateway (collector, poller, the Anthropic batch provider, batch ids by a migration, the deadline), lifted concurrency and pass claims in batch mode, the worker's sit-out, the digest wait ending at the deadline, topics' digests written in parallel. | Tests with a fake batch provider. One real batch per call shape. A night on a scratch database through batches: rounds, times, cost against the projection, the journal against Anthropic's usage, a killed and restarted run that submits nothing twice. |

## Verification

By [`docs/agents/verification.md`](../agents/verification.md).

| | Claim | Evidence |
| --- | --- | --- |
| V1 | Sonnet's Briefs and Weights are good enough | P1's report (refusals, agreement with Opus per effort, tokens) and the owner's read of the pairs |
| V2 | A refusal falls back to OpenRouter, once | pytest with a scripted refusal: one fallback call, journalled under its own provider; P1's fallback answers |
| V3 | Trimming cuts the bibliography and nothing else | unit tests on real LaTeX and PDF texts from N6; P1's token counts |
| V4 | An unchanged text reuses its pass at no cost | pytest on PostgreSQL: a pass whose text hash matches records the earlier result and makes no call; a changed one calls |
| V5 | Batched steps leave the same records as normal calls | pytest with a fake batch provider across analysis, grading and passes: same records, reuse, refusal, truncation and an expired batch handled as today |
| V6 | A restart submits nothing twice | pytest: stop after submit, run again, the fake sees one submission; repeated on the scratch night |
| V7 | The deadline finishes the night | pytest with a fake clock: cancel, finished answers kept, the rest sent as normal calls, the digests written |
| V8 | The owner's actions do not wait on a batch | pytest: an app action calls at once during an open batch; the service worker leaves the queue alone while a nightly holds the run lock |
| V9 | A night costs what is projected and is done in time | P3's scratch night: cost per role at half price, matched to Anthropic's usage; digests written well before the deadline |
| V10 | The migration is safe | pytest migrating a fresh schema; `db migrate` on a fresh scratch database, counts before and after |
| V11 | Parallel digests write what sequential ones did | pytest: a Monday's dailies and weeklies written in parallel through a fake batch equal the sequential result; each topic's order kept |

## Scope

In: `brief` on Sonnet with the OpenRouter fallback; text trimming; the hash reuse; batches for every nightly Anthropic
call, the writer included, with the deadline and resume; the 02:00 nightly; budgets; the spec amendments.

Out:

- The backfill: its own plan, which will use these batches.
- OpenRouter batching (D2).
- Resuming a dead nightly on start-up (R3).
- `judge`'s model and the volume of Area must reads: revisited after a few weeks of costs.
- Deep cards (the subscription pays).
