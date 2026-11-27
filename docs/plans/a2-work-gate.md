# A2: Work gate

Approved by the owner on 2026-11-23. The build plan for milestone A2 of
[`docs/specs/architecture.md`](../specs/architecture.md), shaped in
[`docs/specs/architecture/a2-work-gate.md`](../specs/architecture/a2-work-gate.md); its folded items are listed
under "A2" in [`docs/backlog.md`](../backlog.md). Where this plan and the spec differ, the spec wins and this plan is
corrected.

## Goal and approach

Every background loop becomes a Worker over one Work gate. Each work item gets a record and a lease, and a write
made under a lost lease never commits. One Model gate serves each process, with the OpenAI adapter. Every gate call
is attributed. The CLI submits work and waits for it. Release links are built on the gate.

The work is in seven stages, plus two tracks that run alongside them:
- **P1** builds the gate beside today's loops.
- **P2 and P3** move the loops onto it: passes and deliveries first, then the scheduler, the nightly and the jobs.
- **P4** makes the Model gate one per process.
- **P5** moves the CLI onto submit-and-wait and turns the attribution check strict.
- **P6** builds release links on the gate.
- **P7** cleans up what was kept for rollback.
- **T1**, the deep track, shares the contracts, keeps one LaTeX parser and updates Pi and the model. P6 needs it.
- **T2** builds the OpenAI adapter, against today's provider interface first and inside the gate from P4.

Stages merge and deploy back to back, with no night between them. One weekday night after the last deploy checks
the whole milestone (V9).

## Facts the plan rests on

Found in a survey of the code at 548857e3:

- **Full-text and Deep passes already have a lease with a fence.** `fulltext_passes.lease_until` is set by
  `store/fulltext.py`'s `claim`, and the writes of a pass's result check `held()`: the pass row `FOR UPDATE` where
  the lease matches. Deep's attempt bookkeeping writes without it.
  Nothing renews a lease; it lasts 30 minutes (`core/fulltext.py`, `LEASE`), a batching nightly stretches it to its
  deadline, and a Deep lease is the deep timeout plus 15 minutes.
- **The background full-text worker opens a run and a gateway for each wave** of at most 4 passes with text.
- **Deep passes make no model call.** They are HTTP jobs on the deep service, accounted in `deep_attempts`. The Deep
  pause is global: 15 minutes after the latest service failure.
- **`inference_calls.run_id` is `NOT NULL`.** `eval classify`, `eval briefs` and `ping` build a gateway with no
  journal. `eval ladders` calls providers directly, outside the gateway and its nesting check.
- **Batch recovery is global.** `open_batches` ignores the run, and an answer found for a dead run is written to that
  run's row, which is then retallied.
- **`automatic_deep` has no switch of its own:** it is true whenever the deep integration is configured
  (`feedback/word.py`).
- **The scheduler holds the schedule lock for the whole nightly,** and the nightly takes the run lock inside it.
- **litwatch polls the archive for text.** Each claim of an awaiting-text pass asks the archive again; the archive
  never calls back.
- **The two LaTeX outline parsers** are `api/mcp_reading.py` (MCP's outline, part to subparagraph) and
  `deep/src/paper.ts` (section to paragraph, with numbering and appendix detection).
- **Contracts declared twice:** the deep submission and its outcome (`integrations/deep.py`), the read format
  (`core/deepread.py`, from `deep/src/read.ts`), section kinds (`api/mcp_deep.py`) and the card shape
  (`api/schemas.py`) on the Python side; `PaperInput`, the `/process` body check, `Job` and `ReadSchema` on deep's.

## Choices made without asking

- **Where the gate lives.** The gate is `gates/work.py` (`WorkGate`), and its tables' SQL is `store/work.py`. One
  migration adds `work_items`: kind, key, scope, state, `not_before`, lease token and expiry, timings, outcome, cost,
  parent and run. It also adds `work_id` to `runs` and `inference_calls`.
- **The lease and fence copy the passes'.** The Work gate generalizes `held()`. Each item has a lease token, renewed
  by a heartbeat every third of the lease. A write unit opened under a work context takes the item's row
  `FOR KEY SHARE`, then checks that the token matches and has not expired, or it fails. A lease change locks that
  row `FOR UPDATE`, so it waits for the unit to end; a submission's plain update does not, so a submitter holding a
  paper lock the unit waits for cannot deadlock with it. Lease changes (take, renew, take back, end) run in the Work
  gate's own units, which are not fenced. Two lock modes on an item's row keep the order: a Worker's unit takes the
  fence (`FOR KEY SHARE`) first, which no submission waits for, then its business locks (the pass's row, a paper,
  a cursor); a write that schedules an item (a submission, `FOR NO KEY UPDATE`) takes the business row it decides
  by first (the pass's or the cursor's) and the item's row last, as inline writers (the nightly, a settle) do. A unit
  that schedules several items takes them in pass id order (or cursor name order). Two writes take no pass row: a
  paper brought back (`submit_restored`, in intake's unit) takes the papers' locks first (`settle_many`) and the
  items after, in pass id order; and a pass Worker's retry after its own error (`_tried_again`) locks its own item
  alone, in a unit fenced on it.
- **The pass claim stays, with its lease.** A pass item claims its named pass through the shared claim (today's SQL,
  narrowed to one id). The nightly's inline passes and a re-run's passes claim the same way. An item that finds its
  pass done or no longer wanted ends with nothing to do; one that finds it claimed by another owner sets its
  `not_before` to that claim's `lease_until` and stays pending. Every caller keeps the pass's own `lease_until`, so a
  pass whose owner is gone, standalone or inline, is claimed again by its item once the old claim lapses.
- **Submitting where work becomes eligible.** Whatever makes a pass runnable submits or reschedules its item in the
  same unit: queueing it, the owner's "run it now", `retry_failed`, a re-run letting go of its passes. P3 does the same
  for a job's creation and every return to runnable. A startup sweep submits what is already queued.
- **Waking.** A submission sends `NOTIFY` in the submitter's unit, which Postgres delivers when that unit commits. The
  gate listens on one connection outside the pool, as the session locks do, and otherwise sleeps until the next
  `not_before`. A 30 s fallback poll covers a dropped listener: a drop delays work and loses none.
- **Process liveness.** Each serving process has a `workers` row with a heartbeat and the kinds it runs. A CLI
  command reads it to decide whether a worker for its kind exists.
- **Grain, as today's code has it:**
  - a full-text pass item is one pass and opens its own `fulltext` run with the journal as today (the wave of 4
    becomes a wave of one), so every call keeps a run; from P4 each call also records its item;
  - a job chunk is 50 papers, and its run is the job's, opened at its first chunk;
  - a delivery is one `deliver` call for one integration. Setup, cursors and retries stay as they are, and dispatch's
    backoff (30 s doubling to 1 h) is the item's `not_before`. A delivery that may have left a backlog, read nothing
    behind the outbox's snapshot, or saw an event arrive while it ran submits the integration's next turn;
  - an awaiting-text pass sets `not_before` 30 s ahead, today's tick.
- **The run lock is still taken inside the nightly item and each job chunk,** on its listed session connection. The
  scheduler no longer holds a lock across a night. A nightly item that finds the run lock taken gets a `not_before`
  and stays pending.
- **The Deep pause stays global,** as a `not_before` on every Deep pass item.
- **One Model gate per process,** built by its owner (`serve`, or a test). The batchers' flushing and polling tasks
  are shared by many items, so they run under the gate's system attribution, unfenced, and journal by the original
  call's id; a write that advances a live item stays fenced. `recover` at startup accounts for batches whose items
  are gone and leaves a live item's batch to it. `eval ladders` goes through the gate instead of calling providers
  directly.
- **Attribution** is a second context variable beside A1's `_unit`: a work item, a request or system. The check sits
  where the nesting check does: the Database gate's `read`, `write` and `held`, and the Model gate's `complete`,
  `ask` and `refused_throughout`. It turns strict in P5, once every caller sets it.
- **The CLI submits through the database, not HTTP.** It writes the item to the database `serve` works from, with
  write access (the evaluations open it read-only today), and waits on its row and on `NOTIFY`. An evaluation's
  inputs and candidate settings go in the item's scope; each result is a row in `work_results`, which the command
  reads and writes to its jsonl as it arrives. Resuming works as today: the command skips what its files hold and
  submits the rest, and the replay keeps `--resume`. An evaluation's calls are journalled as evaluation calls, which
  production's reuse and price estimates do not read. `docs/agents/dev-host.md` says how to run them. One command
  is the exception: `intake archive --here` runs in the command, with no item, for a database no `serve` works
  from (a fresh sandbox's build); it calls no model.
- **Until P5, the CLI's inline commands run as items in their own process.** P3 retires the presence session, so a
  command that still runs work inline (`nightly`, `digests write`, `fulltext`, …) does it as a work item through an
  in-process Worker, whose lease gives its run liveness. P5 sends the same items to `serve` instead.
- **Rollback is a handoff of live work.** Until P7, P2 and P3 keep writing the claim state the previous image reads:
  the passes' leases, job states, the old liveness columns (`backend_pid`, `backend_start`). A version switch stops
  `serve` first; the receiving version resumes from the current pass, job, run and cursor records (the old image takes
  back sessionless jobs and lapsed passes; the new one's startup sweep submits what is queued or runnable). P2 and P3
  each rehearse upgrade, rollback and upgrade again with unfinished work on a migrated sandbox. The pass sweep only
  makes sure an item exists (`store.work.ensure`; the delivery sweep reschedules its turn through `submit`): one the
  old image left keeps its time and state, so a pending item waits out at most the claim, retry or pause it was set
  for, and a running one is taken back as its lease lapses. Only its priority is raised to its pass's when that is
  more urgent: the old image raises a pass's priority on the pass row alone (the owner's request for an automatic
  Deep pass), and the item would otherwise wait behind automatic ones. A switch is not made while a nightly runs. One interrupted
  anyway is run again by its fire's item once the receiving image takes it back (a run cut off served nothing); the
  image before counts it served, as a nightly whose process dies does today, and its work waits for the next night or
  a nightly run by hand.
- **Rolling back from P3: the CLI's command items, ended by hand.** The image before admits one running item per
  admission, whatever its lease, and takes back only its own kinds: a P3 `command` item its process left running
  (`fulltext --deep` under `deep`, `publications` under `publications`) would hold that admission there for good.
  P3 ends one wherever it runs (the next command for its admission, any `serve`'s take-back), but not one left as
  the switch is made. So the switch to the image before is made with no CLI command running, and after `serve`
  stops and before the image before starts, the command items left are ended (in the stack's terms):

  ```sh
  dc exec -T litwatch-db psql -U litwatch -d litwatch -c "UPDATE work_items SET state = 'ended', outcome = 'failed', ended_at = now(), error = 'its process stopped', lease_token = NULL, lease_until = NULL, resubmitted = false WHERE kind = 'command' AND state <> 'ended'"
  ```

  The rehearsal shows it: a `fulltext --deep` killed, the rollback with this step, then the image before claims a
  pending Deep pass. Chosen over a row shape the image before does not count (the shared admission in a column of
  its own): kept race-free between a command and `serve`, the admission index must cover that column, and then it
  fails the image before's claim on the same stale row.
- **Rolling back from P5: the commands' items, ended by hand.** P5's command items are of kinds `command:<name>`,
  which the image before neither serves nor takes back; one left running holds its admission (`deep`, `publications`)
  there for good, and one pending never runs. So the switch is made with no command waiting, and the step above ends
  them with `WHERE kind LIKE 'command%' AND state <> 'ended'` in place of `kind = 'command'`. Their `work_results`
  rows and their items' `result` and `cancel_asked` (migration 0051) the image before ignores. Before the switch, the
  evaluations' items are drained: on the host that holds their outputs, each item an output's sidecar names
  (`<file>.item`, a ladders directory's `item.json`), ended and failed ones included, has its results written into its
  files, by `litwatch work follow ID` or by running its command again, and an open one is followed to its end. The
  image before reads no `work_results`: a detached run's results, a ladders run's charges and reservations among them,
  would otherwise sit only there, and its calls be paid for again.
- **Rolling back from P5: the evaluations' calls count in P4's reuse and estimates.** P5 journals an evaluation's
  calls on its run, of kind `eval`, which its production reads leave out (`store.calls`); the image before has no such
  filter, so after a rollback it reuses an answer an evaluation got for an identical request, counts an evaluation's
  refusals when it asks whether every model of a role refused a question (`refused_throughout`), and counts evaluation
  calls in its price estimates. Accepted as P4 caveats: an evaluation asks the request production would, so a reused
  answer is one the same role gave, validated as production validates it, and a refusal is one the same model gave the
  same question, which production would only ask again to be refused; and an estimate is a mean the next nights' calls
  dilute. A filter that P4 alone would read was not worth a step of its own.
- **Rolling back from P4: a reservation the image before does not count.** P4 counts a call's unknown charge at its
  reservation (`inference_calls.reserved`, migration 0050) against its job's ceiling; the image before counts it as
  nothing. So after a rollback, a job whose runs have an unknown charge can spend past its ceiling by that charge's
  reservation. Accepted: unknown charges are rare and the overspend is bounded; nothing is backported.
- **Rolling back from P4: a stored stop the image before does not consume.** P4 settles a batch's refusal or
  truncation on its row, which the item's next attempt takes (`CallJournal.in_batch`); the image before skips a row
  with an error. So after a rollback, a refusal or truncation P4 settled and no attempt consumed yet (the nightly died
  first) is asked for again, paying for that one request twice. Accepted, as the reservation above: rare and bounded.
- **The scheduler's handoff.** A fire the old scheduler already served (a run started after it) stays served when P3
  starts inside the catch-up window: the submitter checks the runs before using the key. A run of the image before
  (`runs.work_id` unset) serves its fire by starting, as it did there; a run of this image only once it closed without
  an error (`store.runs.last_served`, `served`), so a nightly run by hand that failed leaves the fire due. Once
  submitted, a fire's item runs its job unless such a run started since the fire: one by hand, or the item's own
  earlier attempt whose end was lost. An attempt cut off (the database lost, its
  close failed, its process or `serve` stopped) served nothing, so the retry runs the job again, whose steps take only
  what is still due. An expression that never fires again, or never fired (one bound to a year), is left out.
- **Release links:**
  - a `release_links` table holds paper, version, url, kind, state, license, `checked_at` and `found_by` (the
    finder, a re-check or the owner's action);
  - on the paper's page they show as chips coloured by state (`web/src/components/paper/Actions.tsx`, `linkCells`),
    and a Deep card's Code link stays where no release link exists;
  - an automatic find is keyed by the paper's identity (its version, or its update when none is known), which
    keeps one open at a time; its saved answer, "none found" included, keeps later automatic finds of the same
    identity from running; the owner's action runs a fresh find, one open at a time;
  - re-checks use GitHub's and Hugging Face's APIs;
  - the automatic Deep read switch is `[integrations.deep] automatic`, which sets `word.Rules.automatic_deep`;
    unset keeps today's behaviour, and production sets it `false`.
- **Pi** goes to the newest release at build time that knows the GPT-6.2 models: 1.6.0 (2026-11-20) today.
- **The folded re-trigger fix goes with the jobs worker in P3:** an owner's Deep request bumps a running Process job
  only when the job's steps are affected.
- **Shipping.** Each stage is a hard-class PR with drive's review loop; Opus implements and Sol reviews. Stages merge
  and deploy back to back. While P2 and P3 move the loops, unrelated backend PRs stay out of the rollout; T1 and T2
  are part of it.

## Structure

```text
submitter's write unit ── store.work.submit(key, kind, scope, not_before) + NOTIFY ──▶ commit
                                                                                     │
WorkGate (one per process) ◀── LISTEN / next not_before / 30 s poll ─────────────────┘
  admit: one per kind (per integration for deliveries); run lock still inside nightly and chunks
  lease ─▶ Worker sets context {item, lease, ceiling, deadline, attribution}
             └─▶ Service ──▶ db.write(...)  fenced: item row FOR KEY SHARE, token checked
                         └─▶ ModelGate.ask(role, request)  limits, batchers per (provider, deadline)
  heartbeat; lost ─▶ cancel task; expired ─▶ taken back, tried again
CLI ── submit + wait (row, NOTIFY, work_results) ── refuses without a worker row for the kind
```

## Decisions

- **D1. We build the queue ourselves,** a table and about 800 lines with tests. A submission is a Repository write
  in the caller's typed unit, the fence runs inside our write units, and A3's lanes and A4's records sit on the same
  rows; the passes' leases are most of the pattern already. Chosen over procrastinate, an established Postgres queue
  on psycopg 3, whose own connector and schema would fight submitting inside our unit and fencing our writes, and
  around whose rows attribution and A4's records would have to be wrapped.
- **D2. deep's TypeBox schemas are the source of truth** for the shared contracts. deep already declares its read in
  TypeBox and generates JSON Schema from it for the web app (`npm run read-schema`). The job's input and result and
  the release-link answer are declared and generated the same way; Python's models are generated from the schema
  (datamodel-code-generator), and CI fails on a stale generated file. Chosen over Python's pydantic models as the
  source, which would invert the read schema, deep's own, and make deep's validators a second copy.
- **D3. Python's LaTeX outline parser survives,** and litwatch sends the sections in the deep job's input. deep's
  `paper.ts` parser goes; its numbering and appendix detection move into the Python parser, which MCP then gains.
  deep's PDF parts (`pdfParts`) stay: they are not LaTeX. Chosen over deep's parser behind an endpoint, which would
  break MCP's outline whenever deep is down.
- **D4. A2 is built for one `serve`** (the owner, 2026-11-25, after P3's third review round). It keeps correct:
  one `serve` whose Workers run concurrently, CLI commands run beside it, crashes and restarts (take-back once a lease
  lapses), and the rollback switch to the previous image, which stops `serve` first. It need not handle, until after
  A7: two `serve`s, CLI commands racing each other or the nightly's start, and a commit whose acknowledgement is lost.
  There an extra run, a wait or an error is acceptable; lost or corrupted data is not. P3–P7 are built and reviewed to
  this bar, and what falls outside it goes to the backlog ("Work with more than one writer"). Chosen over closing
  every race between writers now, which kept P2's and P3's reviews open round after round; once every kind runs on
  the gate, those races can be closed in one place.

## Risks

- **R1. Five loops replaced, and the nightly breaks.** Handled by a strict sandbox nightly on the same window before
  and after (V3), each kind run end to end on the sandbox (V2), rollback as a rehearsed handoff of live work until
  P7, and one checked weekday night after the last deploy (V9).
- **R2. The fence's row lock makes heartbeats or takeovers wait.** Every write unit of an item holds its row
  `FOR KEY SHARE`, and a renewal locks it `FOR UPDATE`. Units are short since A1, the slow-unit warning surfaces a long one, and a
  heartbeat every third of the lease makes a wait of seconds harmless.
- **R3. One Model gate's limits slow the owner's work during a nightly.** The owner's passes now share per-provider
  limits with the nightly, as the spec intends (no provider's limit doubled). If an owner pass waits noticeably, the
  fix is A3's lanes and is noted there.
- **R4. Batch recovery credits answers to the wrong place.** Recovery runs once per process; an answer keeps its
  original run and item, and the bookkeeping runs under the system attribution. Tested with a batch left open by a
  killed worker (V4).
- **R5. The deep update changes how Deep reads are written.** Pi 1.0 to 1.6 and the model 6.1 to 6.2. Deep reads are
  off, and switching them back on needs its own shaping (backlog). One real Deep read on the sandbox (V8) and the
  finder (V7) exercise the new versions.

## Stages

| | Stage | Shows before the next starts |
|---|---|---|
| **P1** | **The gate.** The migration; `WorkGate` with submit (a Repository call), keys (a scheduled one holds for good), `not_before`, admission per kind or per integration, leases and heartbeats, take-back, the fence in write units, LISTEN and the fallback poll; the `workers` rows; a Worker runner; the attribution context, not yet checked. Nothing uses it yet. | The gate's tests (V1); CI green. |
| **P2** | **Passes and deliveries.** Full-text and Deep passes become items, submitted wherever a pass becomes eligible and by a startup sweep. Dispatch becomes delivery items per integration, each arranging its next turn. Their loops, wake events, ticks and the Deep and dispatch session locks go. | V2 for these kinds on the sandbox, deliveries to a test webhook included; the rollback rehearsal; then merge and deploy. |
| **P3** | **Scheduler, nightly, jobs.** The scheduler submits fires within their catch-up window, served fires staying served, and its lock goes. The nightly is one item under the run lock. Job chunks are items, the job's run spanning them and its ceiling covering the job. Liveness comes from leases, and the presence session goes; the CLI's inline commands run as items in process. The re-trigger fix. | V2 and V3 on the sandbox; the rollback rehearsal; then merge and deploy. |
| **P4** | **One Model gate.** Providers, limits and batchers per (provider, deadline) in one gate per process; ceiling, deadline and journal from the work context; recovery once at startup (a batch a live claim then held, once that claim's lease lapses); `work_id` on calls; `eval ladders` through it. One task per batch, known by one identity (its first call's id) and registered before its rows commit, with its own deadline; a call with no deadline looks once at a batch (its next look or lookup; none while it is being made), a limit of its own, and one no one followed is settled as at a deadline; a nightly after its deadline drains it; a call sees the batches its gate follows, a live item's included. | V4; V3 again; then merge and deploy. |
| **P5** | **CLI and attribution.** The model and pipeline commands submit and wait; evaluations carry their inputs and stream their results to their files; Ctrl-C asks whether to cancel or detach, and `litwatch work follow ID` picks an item back up. The API, MCP, monitor and CLI set request attribution, and the check turns strict. | V5, V6; then merge and deploy. |
| **P6** | **Release links.** The finder's endpoint and prompt in deep; `release_links`; the triggers (an analysis, a new version, the owner's action); placeholder re-checks; the paper's page, the API and MCP; the automatic Deep read switch. | V7; then merge and deploy with deep's image. |
| **P7** | **Cleanup.** The old liveness columns (`backend_pid`, `backend_start`, written through `store.runs.SESSION`) dropped, now that no image reads them; the held-across-calls list down to the run lock; the spec's status updated. | CI green; merge and deploy; then the night (V9). |
| **T1** ∥ | **Deep track:** the shared contracts (D2), one LaTeX parser (D3), Pi to the newest release, `gpt-6.2-sol` for the Deep read (high effort), the finder's own model setting (`gpt-6.2-sol`, medium). Starts with P1; P6 needs it. | V8; deployed, and rolled back if need be, as a litwatch and deep image pair. |
| **T2** ∥ | **OpenAI adapter:** the Responses API with structured output, the Batch API behind the batcher's interface, prices and bounds. Built against today's provider interface from P1 on; it moves into the gate in P4. | V4's adapter part; merged before P4 starts. |

## Verification

| | Claim | Evidence |
|---|---|---|
| **V1** | A submission commits with its unit or not at all. A second submission with an open key is the same item, and a scheduled key holds after its item ends. Items wait for their `not_before`. Admission is one per kind or per integration. An expired lease is taken back and the item tried again. A write under a lost lease fails, and a takeover waits for an open unit; a heartbeat blocked by a unit goes through once it ends. A failed heartbeat cancels the item's task, and a lost pooled connection leaves the item to be tried again. A worker that exits while its last bookkeeping write fails leaves its item to be tried again. `NOTIFY` wakes the gate on commit, not on rollback, and the poll takes over after the listener drops. | pytest on real Postgres (`tests/test_work_gate.py`), one test per sentence; the fence, takeover and heartbeat with two connections under a timeout. |
| **V2** | Each kind runs as a Worker as before: full-text passes, Deep passes, deliveries, the publications lookup, the nightly, Process, re-run, replay and Write again. A nightly blocked by the run lock stays pending, no chunk starts while a nightly is due or running, and a pass the nightly worked inline leaves no item to run, while one it claimed and abandoned is claimed by its item once the claim lapses. A failed pass retried by the owner runs without a restart. Runs and jobs read as alive or dead across a lease's expiry, the retired sessions, locks and loops are gone, and a first Deep request during a Process job no longer re-queues it. | pytest per kind. On the sandbox: a Process job, a re-run under a $0.50 ceiling, a replay, a Write again, a pass retried from Settings, a Deep request to a dev deep instance and deliveries to a test webhook, their run and work records read back; each change's spend counted. |
| **V3** | The nightly runs as before, through the Worker. | On a sandbox built from the archive's papers, the same seeded nightly (`--no-fetch --limit 25`, `LITWATCH_STRICT_GATES=1`): at the base directly; after P3 and P4 as a scheduled item under a strict `serve`; after P5 submitted from the CLI to a strict `serve`. The run records' counts match, the work records are read back, and no check raises. At most $5 a run. |
| **V4** | One Model gate per process. A ceiling holds across a job's chunks. Calls with the same provider and deadline share batches. A batch left by a killed worker is recovered once and credited to its original call, and a second item's call in a batch still settles after the first item's lease is lost. The OpenAI adapter's normal and batched replies parse and report their cost; through the gate, that cost is journalled on the original run and item. | pytest with fake providers (`tests/fakes.py`) and faked OpenAI SDK objects. At T2, one real normal call and one real Batch of 2 requests, parsed with their cost. At P4, the same calls' cost read back from the journal. |
| **V5** | Every unit and model call is attributed, and a missing attribution is caught. | Tests that a Database or Model gate call with no attribution raises in strict mode and logs an error otherwise, and that startup and the gates' bookkeeping run under the system attribution. The suite in strict mode; V3's strict nightly logs no attribution error; the API tests and the stack smoke test pass. |
| **V6** | The CLI submits and waits, refuses without a worker, survives a restart of `serve` while it waits: the command's item ends saying `serve` stopped, the CLI resubmits it, and the replacement skips the step results already saved; the owner's one-attempt consequences hold (committed failures count again, and a nightly whose run closed before its item ended runs again); and an evaluation resumes from its files. | pytest with a worker in process. On the sandbox, with `serve` on its own port: `nightly --limit 5`, with `serve` restarted while the command waits, whose item ends saying `serve` stopped, resubmitted, its replacement skipping the step results already saved; `eval classify` on 10 labels, stopped and resumed; `ping`; the same with `serve` stopped, which must refuse. |
| **V7** | Release links are found with the right access states and shown; triggers and re-checks follow their policy; automatic Deep reads stay off while the owner's Deep requests still work. | `npm test` with a fake model. pytest of the triggers (once per version, again on a new version, always on the owner's action) and of the re-check schedule, ending after four weeks. A real find on about 10 sandbox papers through a dev deep instance, covering public, gated, placeholder and none found, which the owner looks over. Re-checks against a real placeholder repository. On the sandbox with automatic reads off: an eligible paper queues no Deep read, and an owner's Deep request runs. The paper's page walked at 390 and 1440 px, dark and light. MCP `get_paper` shows the links. |
| **V8** | Python and deep share generated contracts, one outline parser serves both, and deep runs on the newer Pi and model. | CI fails on a stale generated contract (shown once in the PR). Parser tests over the papers both parsers covered. `npm test`, `npm run typecheck`, the stack smoke test. One real Deep read on the sandbox with the new image pair, its resolved model and reviews recorded for the owner. |
| **V9** | Production holds. | The first weekday night after the last deploy: its run record against the week's; the logs searched for attribution, nesting, fence, lease-lost, take-back and pool errors; no stuck items; integrations caught up; every eligible paper of the night has a find with an outcome, "none found" included. |

## Scope

**In:** the spec, and everything under A2 in the backlog: the OpenAI adapter, release links, the shared contracts
with one parser, the deep update, the re-trigger fix, leases and fencing.

**Out,** each to where it belongs:
- lanes, the run lock's removal and the nightly's child items: A3;
- work records on the Jobs page, gate events and retention: A4;
- the Fetch gate: A5 (GitHub, Hugging Face and OpenAI use today's clients until then);
- downloads: A6;
- which roles move to OpenAI: an evaluation after T2;
- release links in the digests and the Notion export: later.

## Notes for later stages

Found while building, owned by the stage named.

- **P2, P3, P5 (from P1): the gate's interface.** `store.work.submit(unit, kind, key, scope=, not_before=,
  admission=, lasting=)` in the caller's unit; `WorkGate(db).run({kind: worker})` serves until cancelled, and a
  cancelled gate gives back each claim it still owns once its Worker has stopped. A Worker returns `Done(cost)`
  (or None) or `Later(at)`, which puts the item back to pending until `at` (a pass claimed by another owner, the
  run lock taken, text awaited). Raising fails the item, with the cost a `TerminalError` carries (P6: a find
  that failed after deep charged for it); only a lost database (a dropped connection, a server
  shutting down or ending the session, an empty pool) or a lost deadlock or serialization conflict
  (`gates.work.conflicted`) gives it back to try again. Settling needs a live lease.
  `LeaseLostError` from a write unit means the item is no longer the task's.
- **P2, P3, P5: keys.** A key is unique within its kind while open. A submission that finds its key pending brings
  it forward to the sooner `not_before`, which is the owner's "run it now". One that finds it running marks it
  resubmitted: when that item ends (done or failed), it keeps its own record, and a fresh pending item with its
  kind, key, scope and admission takes the key, at the sooner of the asked `not_before`s, so an owner's retry
  during a run is not lost. When it goes back to pending instead (`Later`, given back as the gate stops, taken
  back after its lease expired, a lost database), it is the same item running again, at the sooner time. A delivery
  arranges its next turn by submitting its own key while it runs, and each turn has its own record. A scheduled fire is `lasting=True`, keyed by its fire time: its key dedupes
  submissions for good, so a fire is never submitted twice, while a lease that expires still retries that same item.
  A delivery passes `admission=f"deliver:{integration}"`.
- **P2: two leases on a pass.** The item's lease is 3 minutes, renewed every minute (`gates.work.LEASE`). The
  pass's claim keeps its own: 30 minutes for an ordinary full-text claim, 90 by default for a Deep one, and a
  batching nightly stretches its full-text claims. A pass item that finds its pass claimed by another owner (a dead
  process's claim not yet lapsed included) returns `Later(lease_until)` at the pass's stored `lease_until`, as the
  plan says, which covers all of these; "ends with nothing to do" is for a pass done or no longer wanted.
- **P2: a lost database reaches the gate from a delivery.** Only a Worker that raises a lost database gets its item
  back to try again, but `events/dispatch.py`'s `deliver` catches every error of `integration.handle` and records
  it as a delivery failure with its backoff. Ported to a Worker, it lets a lost database (`gates.work._lost`)
  propagate and keeps the backoff for the integration's own errors. Done in P2, with `_lost` made public as
  `gates.work.database_lost`.
- **P2 (hindsight from P1's review): one task fewer per claim.** Done in P2: the run loop owns each claim through
  its commit and starts the item's supervisor with it. One shielded task per take is left, so a claim committing
  as the gate stops is still given back.
- **P7 (from P2): deliveries are served by name.** `WorkGate.run(workers, keys=)` serves a kind for some keys alone;
  `serve` gives it the integrations it loads, so a process takes, takes back and waits on only its own deliveries.
  An item no process serves keeps its state: one pending is never taken, and one `running` whose lease expired is
  never taken back. One dropped from the configuration leaves it for good, which P7's cleanup could end.
- **P7 (from P2): failure text.** Errors from outside go through `store.text.stored` before a text column
  takes them. The outbox's, the passes', the jobs' and, since P4, `runs`' and `inference_calls`' do; `digests`
  and `failures` still cut text alone.
- **P7 (from P3): each job write that can change whether or when it runs arranges its chunk.** A job's chunk
  is a `job` item keyed by its id; such a write (its creation or queueing, a claim, progress, a wait, a finish or
  failure, a release) arranges its next chunk in its unit, from the job's row it locked (`store.jobs.arrange`,
  `submit`); a run link or a digest settling changes neither, so arranges none:
  submitted if the job may run, and, written by the job's own chunk, after forgetting what was submitted while it
  ran, so the chunk's item ends with the decision of the job's last write. An error the chunk's handling did not
  record fails the job, which arranges none (`JobWorker._fail`); closing a run only records it. The lock order is
  the job's row, then its item's, a settle's paper locks before either.
- **P7 (from P3): the old liveness columns.** `runs.start` and `jobs.claim` still write `backend_pid` and
  `backend_start` (`store.runs.SESSION`) and `jobs.finish` and `jobs.fail` clear them, for the image before the
  Work gate; jobs keep their states. Nothing in this image reads them. Migration 0049's columns
  (`runs.lease_token`, `jobs.run_id`) are additive: the image before ignores them. P7 drops them with `SESSION`. The held-across-calls list is down
  to the run lock already (the `schedule` lock went in P3, with `held`'s key argument).
- **P7 (from P3, settled in P5): runs and their items.** A run is alive while the claim it runs under holds a live
  lease (`store.runs.ALIVE`, `runs.lease_token`, moved as each chunk links its job's run, migration 0049);
  `runs.work_id` and `work_items.run_id` stay as the history of which item ran what. A run opened outside any item
  reads dead at once: only the API's grade now (`api/settings.py`, kind `grading`), which P5 attributes as its
  request and leaves outside an item, as nothing reads its liveness.
- **P7 (from P5): the CLI's commands.** A command `serve` runs is an item of kind `command:<name>` with a fresh
  uuid key (a replay's, its run's id), its inputs in the scope (`jobs.commands`), admitted by its key or by the kind
  whose work it does (`deep`, `publications`); every `serve` serves them whatever its schedule (one without an archive
  or a deep service not those that need it), under its own configuration, so the commands lost `--config` but for the
  evaluations, which build their requests here. The command refuses when no `workers` row lists its kind and beat
  within a lease, follows the item read-only (its row, `work_results` after the last id it showed, on `NOTIFY` of both
  channels or every 2 s), and holds no claim. Its progress is the item's `work_results` (an evaluation's answers and
  reservations, as lines of its files); what it returns is the item's completion record (`result`), written in the
  unit that ends it. A command's item gets one attempt: a take-back, a give-back or a stop of `serve` ends it failed,
  `serve stopped while it ran` (`store.work.STOPPED`), and the command, seeing that error, waits until a `serve` runs
  the kind again and submits a fresh item with the same arguments (an evaluation's with what its files still lack).
  Running again goes on from what committed: the nightly's steps, the passes, an evaluation's files and records.
  `--limit` holds per item, so a resubmission counts afresh. A stopping attempt lets go of what it holds as P3's
  shutdown does: its run is closed by the run's own cleanup, and a command's text passes, claimed outside a run, are
  released as the wave is cut off. The serving check is made in the submit unit, on the `workers` row locked `FOR
  SHARE`, so a retirement and a submission have a defined order; a `serve` that registers replaces every earlier
  rows. A replay has one item open at a time, keyed by its run, and `--resume` follows the one still open before it
  estimates or submits. A cancel (`store.work.cancel`) is a request on the item (`cancel_asked`), notified: a pending
  item ends at once; a running one's supervisor stops its Worker once, lets its cleanup (its run closed) finish under
  the claim, then ends it failed, so its admission is free only once it has stopped. Only a command's item has a
  stopper, and only one is followed or cancelled from the CLI. Each evaluation's output names the item writing it
  (`<file>.item`, a ladders directory's `item.json`): a rerun first writes in that item's results, its reservations
  included, so a follower that stopped loses nothing, and follows the item first while it is open. `work_results` rows
  are kept with their item; P7's cleanup may prune an ended item's. `intake archive --here` is no item: it runs in the
  command, with no Work gate and no Model gate, for a database no `serve` works from (the sandbox's build). A gate
  that stops retires its `workers` row first, before its items drain, so no command is accepted for a kind no one will
  run; a `serve` killed leaves its row until the next `serve` replaces it or its heartbeat is a lease old
  (3 minutes at the default lease), and a command submitted meanwhile waits for the next `serve`. No CLI builds a
  Model gate any more: P4's findings on a second gate meeting `serve`'s batches (`owner()` sees only its process) need
  no fix while that holds, and `jobs.context.model_gate` serves `serve` alone.
- **P7 (from P5): the P3 `command` kind.** `store.work._retire_commands` still ends an expired P3-image `command`
  item at every take-back; P7 drops it with the rollback steps.
- **P7 (from P5): the nightly and the chunks.** No chunk starts while a nightly waits (`store.jobs.nightly_waits`,
  P3's rules): a CLI's `command:nightly` item counts always, a scheduled fire's only where this process's
  configuration runs fires (`fires=`).

- **P7 (from P6): release links keep nothing for rollback.** Migration 0052 (`release_links`, the `work_items_finds`
  index) is additive; the image before serves neither `links` nor `link check`, so their items wait for this one. A
  find's `work_results` row, its answer or its failure, is what a retried find reads to end without asking deep again
  (`links.record`), and what says the find answered while its item is open; once the item ends, its own `result` says
  so (`store.links.finds`), so P7's cleanup may prune the row with the ended item's other results.
- **Rolling back P6 turns automatic Deep reads back on.** The image before P6 ignores `[integrations.deep]
  automatic`: rolled back with `automatic = false` installed, it writes automatic Deep reads wherever deep is
  configured. The rollback step is a choice: keep deep configured and accept the automatic reads until the image is
  upgraded again, or disable `[integrations.deep]`, the only way the previous image stops them, which also pauses
  the owner's Deep read requests until deep is enabled again.
- **Back up to P6 after a rollback that spanned a nightly.** The image before P6 finds no links: the analyses and
  versions it recorded triggered no find. Upgraded again, nothing submits them at once. The next nightly's `due`
  sweep covers only the papers it analyses that night and the failed finds of the last week, so a paper analysed or
  versioned under the old image is not found by it; the owner's "Find links" covers those, one paper at a time. No
  catch-up is built.

## Evidence

Filled in as stages land.
