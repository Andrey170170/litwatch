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
  production's reuse and price estimates do not read. `docs/agents/dev-host.md` says how to run them.
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
  - an automatic find is keyed for good by paper and version, a "none found" outcome included; the owner's action
    always runs a fresh find;
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
| **P4** | **One Model gate.** Providers, limits and batchers per (provider, deadline) in one gate per process; ceiling, deadline and journal from the work context; recovery once at startup; `work_id` on calls; `eval ladders` through it. | V4; V3 again; then merge and deploy. |
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
| **V6** | The CLI submits and waits, refuses without a worker, survives a restart of `serve` while it waits, and an evaluation resumes from its files. | pytest with a worker in process. On the sandbox, with `serve` on its own port: `nightly --limit 5`, with `serve` restarted while the command waits, which follows the same item and writes nothing twice; `eval classify` on 10 labels, stopped and resumed; `ping`; the same with `serve` stopped, which must refuse. |
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
  run lock taken, text awaited). Raising fails the item; only a lost database (a dropped connection, a server
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
- **P4: work context.** `gates.context.current_work()` gives the item, with `ceiling` and `deadline` left
  `None` for P4 to fill.
- **P4: a batch's outcome is settled in pieces, not once.** T2's review found five edge cases, each rare:
  a batch can still be created after the deadline, since `staged()` checks the clock before its row writes, not
  after (`llm/batches.py:292`);
  the window for finding a batch whose creation may have failed can start late, behind the poller
  (`llm/batches.py:314`, `:433`);
  a definite batch rejection (a 400, or a connection that never opened) keeps a null charge, not 0, and so does a
  per-request error (`llm/batches.py:335`, `store/calls.py:160`, `llm/openai.py:228`);
  with its waiter cancelled, or in a recovery nobody attaches to, a batch refusal's stop and category are never
  persisted (`llm/batches.py:490`, `llm/gateway.py:457`);
  with the original waiter cancelled, a surviving shared waiter cannot save its validated output on
  `source_call_id` (`llm/gateway.py:496`, `:542`).
  The reviewer's shape, for the gate: one journal settlement operation that takes usage, a failure or a stop, and an
  explicit reply flag. The batcher persists that outcome and gives waiters the source row's ID, and the gateway
  validates and saves the output on that row.
  The PR's bot review added two more for the same operation: a journal failure partway through `staged()` leaves
  the rows it already opened without a batch ID or a settlement (`llm/batches.py:298`); and an unknown charge
  keeps its ceiling reservation only in the run that made it, so a resumed re-run, which starts from the job's
  known spend (`jobs_store.spent`), no longer counts it (`llm/gateway.py:276`). It belongs with the ceiling
  across a job's chunks (V4): count an unknown charge at its reservation until it is known.
- **P5: request attribution.** `gates.context.Attribution` has `work` and `system` constructors; P5 adds the
  request one and the check. The `workers` rows are written and beat, but no one reads them yet: P5 adds the
  CLI's reader, which counts a row whose `heartbeat_at` is within a lease as alive.
- **P4 (from P2): four text gateways.** Text-pass admission is spread over `TEXT_WAVE` slots, and each text item
  opens its own `fulltext` run and gateway, so four at once are four gateways until P4's one Model gate.
- **P4, P7 (from P2): failure text.** Errors from outside go through `store.text.stored` before a text column takes
  them. The outbox's, the passes' and, since P3, the jobs' do; `runs`, `inference_calls`, `digests` and `failures`
  still cut text alone, for the stages that move them.
- **P4 (from P3): a job's ceiling across its chunks.** A chunk's run is opened at the job's first chunk in a
  process and kept across its chunks there (`JobWorker.open`); its ceiling is set once, when the run opens, to the
  job's ceiling less what all its runs spent (`reruns.ceiling`, `jobs_store.spent`). A chunk on another process, or
  after a restart, opens a run of its own and starts from that spend. A process reuses the run it kept only while it
  is still the job's latest (`jobs.run_id`, written as each chunk links its run): once another process's chunk ran
  the job, the kept run is closed and the next chunk here opens one from the spend, so chunks going back and forth
  between processes never spend past the ceiling, and a run kept for a job that moved on holds none of `OPEN_MOST`.
  P4's ceiling from the work context keeps this reading: the item is the chunk, the ceiling is the job's.
- **P4, P5 (from P3): runs and their items.** A run is alive while the claim it runs under holds a live lease
  (`store.runs.ALIVE`): `runs.lease_token` is the lease token of the item it was opened in, or, for a job's run, of
  its latest chunk's, moved as each chunk links it (`store.work.link_run`, migration 0049). An item taken again gets
  a new token, so it revives no run of the claim before. `runs.work_id` and `work_items.run_id` stay as the
  history of which item ran what: each run records the item it ran under (`runs.start` sets `work_id`, a chunk's
  run its chunk's), and an item its latest run (`work_items.run_id`), so a chunk taken back keeps the run before.
  A run opened outside any item reads dead at once: today only the API's grade now (`api/settings.py`, kind
  `grading`), whose liveness nothing reads. P5's request attribution can give it one, or say why not.
- **P5 (from P3): the CLI's commands are items in their own process.** `gates.work.inline` submits a `command`
  item keyed `{name}:{uuid}`, admitted by its own key or by the kind whose work it does (`deep` for
  `fulltext --deep`, `publications` for the lookup), waits for its admission, and runs that one claim itself under
  the gate's heartbeat (`WorkGate._admitted`, `_once`); no gate serves it. It has one attempt, as a command run by
  hand has: a lost database or a lost conflict as it claims or works, a lost lease, or work that stops cancelled ends
  it failed with a message to run it again (`CommandCutOffError`), and it is never given back; it returns once its
  end is recorded done. Whatever stops it after the submit (Ctrl-C, an error of the gate's, its end not recorded),
  its `finally` ends the item failed by its key (`store.work.abandon`), so none is left pending or running. Any
  `serve`'s take-back ends failed a `command` item whose lease lapsed, whatever its admission. P5 replaces this with
  submit and wait (spec, "The CLI"), where `serve`'s gate tries again. The callers to move:
  - [ ] `intake`, `intake --catalog-from`, `intake archive` (inline today)
  - [ ] `nightly`, `digests write` (inline today)
  - [ ] `fulltext`, `fulltext --deep` (inline today, the latter under the `deep` admission)
  - [ ] `publications` (inline today, under the `publications` admission) and `publications --paper` (not inline)
  - [ ] `eval replay` (inline today)
  - [ ] `ping` (not inline)
  - [ ] the unjournaled evaluations: `eval classify`, `eval digests`, `eval briefs`, `eval ladders` (not inline)
- **P5 (from P3): the nightly and the chunks.** No chunk starts while a `nightly` item is pending, or running
  without having closed the run that claim opened (opening it included; a lapsed lease too, until the item is taken
  back), or a nightly run is open and alive (the CLI's) (`store.jobs.nightly_waits`). The item clauses count only
  where this process's configuration runs fires (`fires=`, the schedule on): a fire left pending by a `serve` since
  started with the schedule off holds back no job, re-run pass or `fulltext --deep`. A close that fails leaves the
  run open and the fire's item running: it is tried again on the same fire after a backoff (`jobs.scheduler.WAIT`,
  doubling, at most an hour), and the chunks wait until it closes; a chunk reads that before the run lock and again under it, before
  it claims its job, and a waiting one looks again after `jobs.worker.WAIT`. The nightly releases every runnable
  job's chunk in the unit that closes its run, failed or cut off too (`store.jobs.release`, `model_run(closing=)`),
  so they go at once. Tonight's run and that release both take the `tonight` lock, so one of them sees the
  other, each stamped by the clock after taking it (`jobs.queued_at`, `runs.finished_at`: `clock_timestamp()`, not
  the transaction's start, which a unit waiting on its fence or the lock has long passed). `serve`'s sweep submits
  the fires due before its gate starts. A fire's publication and a chunk's claim are one decision: both take the
  `fire` lock, and the claim, under it and after its job's row, counts a nightly fire due but not published yet as a
  nightly that waits (`jobs.scheduler.unpublished`), so either the nightly goes first or it waits that one chunk.
  Once a nightly that took the run lock lets go of it, it brings forward the chunks it held back
  (`jobs.nightly.wake`); one that never took it wakes none. The release and the wake are one operation per job
  (`store.jobs.submit`, `how=`), in the order of the jobs' ids, each the job's row and then its item's; the wake
  moves only a pending item.
- **P5, P7 (from P3): each job write that can change whether or when it runs arranges its chunk.** A job's chunk
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

## Evidence

Filled in as stages land.
