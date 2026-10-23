# Jobs

Approved by the owner on 2026-10-22. This plan builds the jobs API and the Jobs page: the work litwatch runs for the
owner outside the nightly run. The spec, [`docs/specs/v1/`](../specs/v1/README.md), says what it does. This plan
amended [DESIGN.md](../specs/v1/DESIGN.md) (Process, Jobs), [archive.md](../specs/v1/archive.md) (Process) and
[models.md](../specs/v1/models.md) (the estimate's basis). Where this plan and the spec differ, the spec wins and this
plan is corrected. Terms are the glossary's.

## Goal and approach

v1 shipped without the jobs API (deferred in [v1-build.md](v1-build.md), "API gaps W2 found"). Settings › Jobs, the
topic editor's re-run offer and Overview's Run replay are placeholders. Process on a paper outside the collection only
fetches it: the paper waits for the next nightly run ("Fetched · next run"), and nothing in Jobs shows it.

A `jobs` table is the owner's queue. A worker in `litwatch serve` runs it in chunks, under the run lock, giving way to
the nightly run. `/api/jobs` lists and acts on jobs, and the Jobs page shows them in DESIGN.md's four groups. Four kinds
of work become jobs: Process, a topic's scoped re-run, a replay, and a digest written again. Deep cards asked for keep
their own queue and are shown beside the jobs.

```
Process · re-run offer · Run replay · digest Write again
  → POST /api/jobs → jobs → worker in serve (chunks, run lock, gives way to the nightly)
  → classify / analyse / grade / digests → runs rows, outbox
  → replay → evaluation tables only (a dry run)
card passes (reason request) → GET /api/jobs, read alongside → Jobs page, Runs and cost
```

## Choices made without asking

- **A job is a row in a new `jobs` table:** its kind, its scope (paper ids, a topic and its version, cohorts, a
  digest), its state, the paper count and estimate when it was offered, its ceiling, what it has spent, how far it
  got, its error, and the run records it made. Each time the worker runs a job (from claim to stop, done or failure)
  is one `runs` row of kind `job`, however many chunks it takes, so its cost reaches the month's spend as every
  run's does. The Nights table and Overview's latest night read nightly runs only (`store.quality.runs` filtered by
  kind; today it takes the newest 30 of any kind), so jobs never push nights out of it.
- **States:** `offered` and `stopped` (Not run); `tonight`, `queued` and `running` (Running and queued); `failed`;
  `done`; `discarded` (hidden). Each of the page's four groups is one set of states. `stopped` is a job its ceiling
  stopped: it waits under Not run with what is left, and Run now continues it under a new ceiling.
- **The worker runs in `litwatch serve` under `[schedule] enabled`,** as the full-text and Deep workers do
  (`api/app.py`'s lifespan): every worker that calls models waits behind the one switch, and a Process job's Brief or
  Deep card needs those workers anyway. It claims a job with `FOR UPDATE SKIP LOCKED`, so two servers on one database
  never run the same job. The end-to-end checks run a sandbox `serve` with the schedule on.
- **It gives way to the nightly run** (pipeline.md, section 10). It works in chunks of about 50 papers and takes the
  run lock for each chunk. It starts no chunk while a nightly run is running or due, so the nightly waits at most one
  chunk.
- **It chooses again between chunks.** After each chunk the worker takes the next chunk from the most urgent
  runnable job, not from the job it just worked: Process jobs first (the owner waits on them, D1), then the others
  oldest first. A large re-run therefore delays a Process job by at most one chunk; both stay `running` meanwhile.
- **A dead job is taken back,** like a dead run. The job records its backend pid and start time. The worker puts a
  `running` job whose session is gone back in the queue. Steps choose their papers by what is still due, so a job
  taken back skips what it already did.
- **The estimate** is calls by role times the mean cost of the role's model over its last 200 calls
  (`store.calls.mean_cost`, which `replay.estimate` already uses). It is shown before Run; it does not enforce
  anything.
- **The ceiling is hard** (models.md): the job's gateway holds a budget. Before each call, retries and fallbacks
  included, it reserves the call's most it can cost (its input tokens at the input price plus its maximum output
  tokens at the output price) against what is left. A call that does not fit is not sent, and the job stops as
  `stopped`, recording where ("stopped at the ceiling after 212 of 476"). When the call returns, its actual cost
  replaces the reservation. The job may stop a little below its ceiling, never above it.
- **Each route's price bound, before the call.** Anthropic: `llm/prices.py`'s table at its worst case (every input
  token at the cache-write price, with the long-prompt factor when the prompt passes its threshold). OpenRouter,
  which reports a call's charge only in its answer: the highest per-token price the model's last 200 calls were
  charged, sent with the call as OpenRouter's `provider.max_price`, so no provider dearer than that bound serves it.
  A route with no price bound (no listed price, no history) cannot run under a ceiling: the offer says so, and that
  role's calls are not sent by the job.
- **A re-run's full-text and Deep passes are background work** (pipeline.md, section 10). They carry the job that
  queued them and a background priority, numerically larger (later) than `TEXT_PRIORITY` and `AUTOMATIC_PRIORITY`
  in `core.fulltext`, since lower runs sooner.
- **A re-run works its own full-text passes, under its ceiling** (the owner's call on the PR's review, 2026-10-22).
  After its grading, the job works the Brief and Weight re-check passes it queued through its own gateway, so their
  calls reserve against the same budget and count in the job's run. The full-text worker never claims a pass that
  belongs to a job. The job stays `running` until those passes are done, failed or stopped by the ceiling; a pass
  still waiting for its text when the job's chunk ends is worked by its next chunk. Its Deep passes go to the Deep
  worker, behind the automatic ones, and stay outside the ceiling: Deep runs on the subscription, not on API spend
  (models.md). Neither the job's passes nor its Deep passes run while a nightly is running or due, by the same check
  the jobs worker uses (due only with the schedule on, which alone starts it). A Process job's passes are the owner's request: they keep the request priorities and the
  regular workers, and a Process job has no ceiling.
- **Deep cards asked for are not `jobs` rows.** Their queue is the card passes the deep integration already runs.
  `/api/jobs` reads the passes with reason `request` and puts them in the same groups, so no state is stored twice.
- **The per-paper actions stay as they are:** Grade it now and a full-text pass's Now answer within seconds on the
  paper's own page, and analysis Try again hands the paper back to the next run. The queue would only add a wait.
- **Runs and cost** shows the latest three jobs and "All jobs ›". A failed job that is also a Needs you item settles
  with it (DESIGN.md).
- **The API is typed:** `/api/jobs` lists jobs grouped; a create endpoint per kind answers with the paper count and
  estimate; `run`, `tonight`, `discard` and `again` act on one job. Each has a Pydantic response model, and the web
  client is regenerated.
- **Deep read on a paper outside the collection** starts the same Process job, and its card is asked for as today.
- **Built in P1 beyond the bullets above:** with the schedule off, Process only fetches the paper (no worker runs
  it), and a paper fetched but never processed reads "Fetched · next run"; with the schedule on, that paper gets
  Process, which queues its job now, as the API's `process_now` says (the owner's call, 2026-10-23). Asked again
  while its job is queued or running, Process only answers with that job, and so does Deep read with its card's
  pass, the paper not dismissed; without a taxonomy, Process answers 409 and keeps nothing; a failed Process job is queued again once its
  paper's failures are cleared, and discarded if the paper is dismissed; a card asked for shows as Failed by the
  rule Needs you uses. A failed Process job is tried again (Jobs' Try again, the paper's "Process again") only while
  no step of its paper waits on the owner; then it would find nothing due, so neither is offered and the API
  answers 409 (Deep read queues no job either): the owner's action on Needs you settles it, and on the paper's page
  the button reads "Failed". A Process job that failed after classifying its paper shows on the paper's page as a
  red note with Try again while it can be tried again. A job's gateway sends no batch, but answers a call a dead
  nightly left in an ended batch from that batch, as the full-text worker's does. A Process job whose paper the
  owner acts on while it runs (a Try again on Needs you, say) goes back in the queue when it ends instead of done,
  so its steps take what that action left due; a Process job taken back starts over from its paper for the same
  reason.

- **Built in P2 beyond the bullets above:**
  - *The four scopes, read one way:* labelled papers and reported misses for the topic; its members as its page
    shows them (standing in its class or those under it, or graded for it or a followed topic under it); papers its keywords match over the editor's 90
    days, counted as the editor counts them; papers published in the date range, both days included in the
    schedule's zone, members or not (as the keyword scope). Dismissed papers are left out of each. The ticked scopes
    together count a paper once, and their papers are fixed when the job is made.
  - *The estimate is of grading:* calls per paper by the topic's depth (an Area's `check` by the share of its last
    200 verdicts that escalated). The Briefs a re-run's verdicts bring are paid under its ceiling but not estimated.
  - *A grading that fails in a re-run is the job's, not the paper's:* no `step_failures` row, so no nightly takes it
    up; the job ends `failed` ("grading failed on 3"), Try again redoes what has no verdict under the version,
    each grading as far down its graders' lists as its earlier tries on the same text reached (D4, kept on the
    job; both Focus sides refused asks both again). A grading whose paper's text changed meanwhile counts as
    failed too. A re-run's progress is counted from the verdicts. A
    re-run whose topic was paused or unfollowed fails, saying so; Run is refused (409) with the schedule off.
  - *The worst case of a call:* its input bounded by the request's UTF-8 bytes plus 1,024 tokens of framing (a token
    is at least a byte), its output by its cap. An OpenRouter route's bound is, per token type, the tightest charge
    over tokens among its last 200 calls per serving provider, the highest across providers (a call with cached input
    left out of the input price); a Decisions route is held to its input (`max_price` completion 0, models.md: it
    bills input only); a chat route with no output cap of its own is sent 16,000, Anthropic's default. A call that
    does not fit waits while others are in flight and stops the job only with none in flight. An answered provider
    error frees its reservation, unless it reports a charge (OpenRouter bills some completions with no choices); a
    content refusal OpenRouter answers with a 403 costs nothing; a timeout or a lost connection keeps it, as it may
    have been billed. A known limit: a charge the journal never learns (a timeout, a process that died mid-call,
    or a charge an error reported, which the journal does not keep yet: [backlog](../backlog.md)) is not counted
    against what is left when the job's next run opens, so such a job can pass its ceiling by those charges.
  - *A re-run asked for twice is made once:* the same topic version, scopes and dates while one is not yet ended
    adopts it (under a lock per topic, so two at once make one), and Run moves one not run to the queue (a stopped
    one under a ceiling above what it spent, as Run now on Jobs). Otherwise the one found is adopted only as asked,
    under the same ceiling (queued or running for Run, not run for Not now): else a 409 says where it is. Run now on Jobs is refused with the schedule off
    too.
  - *A topic edited back* to an earlier version re-runs what a version between them decided: a paper is done
    when its latest verdict is under the version, its calls reused from the journal.
  - *A re-run's pass the owner runs from Needs you* ("Run it now") leaves the job for the regular queue, as do the
    passes of a paper whose Process job is under way, which go as the owner's requests. A pass
    the job works settles under the job: the passes and the Deep card it brings are the job's, at background
    priority.
  - *Where it stopped:* "stopped at the ceiling after 212 of 476" while grading, "with 3 full-text passes left"
    after. Run now on a stopped re-run takes a new ceiling above what it spent; the ceiling is over the job's life.
    Discarding it lets go of its text passes; a re-run that ends lets go of those it left (failed, or awaiting text
    a week) to the regular queue. With no archive configured, a re-run stops with its passes kept ("with 2
    full-text passes left: no archive"). A topic edited before its re-run has graded all fails the re-run: its
    verdicts would be under two versions, and the editor offers the new version's. A re-run whose passes all await
    their text is not claimed again for 30 seconds; it keeps its run, a database connection, open meanwhile, so a
    worker holding four open starts only Process jobs until one ends.
  - *The Jobs page* shows a running job's spend as its calls are journalled, and a re-run's as spent of its ceiling.

## Decisions

- **D1. Process runs now.** Process fetches the paper and queues a Process job at once. The paper is classified,
  analysed and graded within a minute or two, and its Brief or Deep card follows through the passes. One paper costs
  a few cents at normal prices. Jobs shows the job running, then done. The owner opens a paper outside the collection
  to read it now. *Rejected:* queuing it with tonight's run (the paper stays bare until morning), and two buttons (one
  more choice on a full action grid).
- **D2. "With tonight's run" makes normal calls after the nightly.** A `tonight` job waits for the nightly to finish,
  then runs as Run now does. The ceiling stays exact. The sketch's re-runs cost $0.06 to $0.61, so half price would
  save cents. *Rejected:* batching inside the night's window, whose cost is known only when the batch settles. That
  is worth revisiting with backfill.
- **D3. A topic re-run redoes grading for that topic only.** A topic edit changes what grading reads (depth, subject,
  rubric, interests, keywords), and the re-run records a new verdict per (paper, topic) under the new version; each
  verdict settles its paper (effective grade, rarity, library, passes). A subject edit also changes the followed
  class's description (`Taxonomy.following()`, taxonomy.md), so it changes classification too, forward, as every
  edit does. Classifying existing papers again after it is the class-edit re-run, which waits for the taxonomy write
  API ([backlog](../backlog.md)). *Rejected:* classifying the scope again in this plan, which costs more and changes
  memberships for every class.
- **D4. The unbuilt parts live in [`docs/backlog.md`](../backlog.md),** one ordered list that `AGENTS.md` and
  `milestones.md` point to. *Rejected:* a section in `milestones.md`, which records what was built and is already
  long.

## Risks

- **R1. A re-run moves many papers at once.** New verdicts change saves, Zotero and Notion pages, and demotion
  archival across the whole scope, and the outbox gets a burst of events. Each paper settles under `word.change`, as
  Grade it now does, and integrations take events at their own pace. The offer row shows how many papers a re-run
  touches before Run.
- **R2. The ceiling stops a job early.** A call's reservation is its worst case, so a job stops while some of its
  ceiling is still unspent, more so with many calls in flight. The stopped job says what it spent and what is left,
  and Run now continues it. If reservations prove far above actual costs, concurrency for jobs is lowered rather than
  the ceiling softened.
- **R3. A job and the nightly compete for the run lock.** The worker never starts a chunk while a nightly is due or
  running. A chunk takes minutes and the scheduler's catch-up window is 6 h, so the nightly is late by at most one
  chunk.
- **R4. A deploy happens mid-job.** The job is taken back and its steps pick only what is still due. A replay resumes as
  `litwatch eval replay --resume` does.

## Stages

Each stage is its own PR, by `drive`.

| Stage | Builds | Shows before the next starts |
| --- | --- | --- |
| P1 | **Core and Process.** The `jobs` migration; its store; the worker (claim, chunks, run lock, giving way to the nightly, take-back); the step functions narrowed to a paper set (`due(…, ids=)`); `/api/jobs`; Process and Deep read starting a Process job (D1); the Jobs page's four groups with Run now, With tonight's run, Discard and Try again; Deep cards asked for read alongside; the latest three on Runs and cost. | An archive paper processed from the app on the sandbox, with Jobs showing it queued, then running, then done. A nightly due mid-job runs first. |
| P2 | **Topic re-runs.** The four scopes (labelled papers and reported misses, the topic's members, papers matching its keywords, a date range), each with its paper count and estimate; the ceiling; Run and Not now in the topic editor in place of its `Later`; a regrade per (paper, topic) under the new version (D3), chosen by the scope rather than by `grading.due`, which skips a paper with a current verdict whatever the topic's version; the job working its own passes under its ceiling. | A rubric edit on the sandbox, re-run on the topic's members with a low ceiling: it stops at the ceiling, Run now continues it, and the verdicts carry the new version. |
| P3 | **Replay and digests as jobs.** Overview's Run replay with its estimate and ceiling, and the stale note on recall per cohort, which is already shown, removed. A failed digest's Write again goes through a job: when the run lock is busy it queues instead of answering 409, and it settles its Needs you item. | A replay from Overview writes only the evaluation tables. A digest written again while a job holds the lock waits, then is written. |

## Verification

By [`docs/agents/verification.md`](../agents/verification.md).

| Claim | Evidence |
| --- | --- |
| V1. A job runs its paper set through classification, analysis and grading, and only that set | pytest on real Postgres with fake providers |
| V2. Two workers never run one job; a job whose session died is taken back and finishes without redoing work | pytest: two claimers; a job left `running` by a closed session |
| V3. No chunk starts while a nightly is due or running; a Process job queued during a re-run runs at its next chunk | pytest with the scheduler's `is_due` and a held run lock; a re-run and a Process job queued together |
| V4. The ceiling is never passed: a call that does not fit is not sent, the job stops as `stopped` and records where; Run now continues it; an OpenRouter call carries its `max_price` | pytest with a fake provider whose costs are known; the OpenRouter request body |
| V4b. A re-run's full-text passes run in the job under its ceiling and never in the full-text worker; none of its passes runs while a nightly is running or due | pytest (P2) |
| V5. A topic re-run records verdicts under the new version for its scope only, and settles each paper | pytest (P2) |
| V6. A replay job writes only evaluation tables | pytest asserting that verdicts and the outbox are untouched (P3) |
| V7. The Jobs page shows the four groups and their actions; Process, the re-run offer and Run replay start jobs | vitest; a browser walk on `serve` over a fresh `--production` sandbox, with screenshots per stage |
| V8. It works end to end with real models | One sandbox Process and one small re-run with real providers, under the $5 evidence budget |

## Scope

**In:** the `jobs` table, worker and API; Process, topic re-runs, replay and digest Write again as jobs; Deep cards
asked for shown in Jobs; the Jobs page, Runs and cost's latest three, the topic editor's offer and Overview's Run
replay; the spec amendments above; [`docs/backlog.md`](../backlog.md).

**Out, on the backlog:** re-runs after a class edit (they need the taxonomy write API); a "re-run not run" hint in the
topic editor (an open question from the sketch); backfill over older windows (after v1, decision 48). **Out for
good:** moving Grade it now and the other per-paper actions onto the queue.
