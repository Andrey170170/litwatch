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
  `store/fulltext.py`'s `claim`, and every write checks `held()`: the pass row `FOR UPDATE` where the lease matches.
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
  by a heartbeat every third of the lease. A write unit opened under a work context takes the item's row `FOR SHARE`
  where the token matches and has not expired, or it fails. A takeover updates that row, so it waits for the unit to
  end.
- **The pass claim stays.** A pass item claims its pass with today's SQL. The nightly's inline passes and a re-run's
  passes claim the same way, and an item that finds its pass done or already claimed ends with nothing to do. A
  standalone pass's lease is its item's; the pass's own `lease_until` stays only for the nightly's inline passes,
  until A3.
- **Waking.** A submission sends `NOTIFY` in the submitter's unit, which Postgres delivers when that unit commits. The
  gate listens on one connection outside the pool, as the session locks do, and otherwise sleeps until the next
  `not_before`. A 30 s fallback poll covers a dropped listener: a drop delays work and loses none.
- **Process liveness.** Each serving process has a `workers` row with a heartbeat and the kinds it runs. A CLI
  command reads it to decide whether a worker for its kind exists.
- **Grain, as today's code has it:**
  - a full-text pass item is one pass, so the wave of 4 per run goes; the item opens no run of its own and attributes
    its calls to itself;
  - a job chunk is 50 papers, and its run is the job's, opened at its first chunk;
  - a delivery is one `deliver` call for one integration, and dispatch's backoff (30 s doubling to 1 h) is the item's
    `not_before`;
  - an awaiting-text pass sets `not_before` 30 s ahead, today's tick.
- **The run lock is still taken inside the nightly item and each job chunk,** on its listed session connection. The
  scheduler no longer holds a lock across a night. A nightly item that finds the run lock taken gets a `not_before`
  and stays pending.
- **The Deep pause stays global,** as a `not_before` on every Deep pass item.
- **One Model gate per process,** built by its owner (`serve`, or a test). `recover` runs once at startup over every
  open batch, and an answer it finds is credited to its original call's run and item. `eval ladders` goes through the
  gate instead of calling providers directly.
- **Attribution** is a second context variable beside A1's `_unit`: a work item, a request or system. The check sits
  where the nesting check does: the Database gate's `read`, `write` and `held`, and the Model gate's `complete`,
  `ask` and `refused_throughout`. It turns strict in P5, once every caller sets it.
- **The CLI submits through the database, not HTTP.** It writes the item and waits on its row and on `NOTIFY`. An
  evaluation's inputs go in the item's scope; each result is a row in `work_results`, which the command reads and
  writes to its jsonl as it arrives. Resuming works as today: the command skips what its files hold and submits the
  rest.
- **Rollback stays possible until the end.** The old liveness columns (`backend_pid`, `backend_start`) and the
  pass's own lease stay until P7, so the previous image can run on the new schema.
- **Release links:**
  - a `release_links` table holds paper, version, url, kind, state, license, `checked_at` and `found_by` (the
    finder, a re-check or the owner's action);
  - on the paper's page they replace the Deep card's Code link (`web/src/components/paper/Actions.tsx`,
    `linkCells`), as chips coloured by state;
  - re-checks use GitHub's and Hugging Face's APIs without a token: about 20 a week fits the anonymous limits;
  - the automatic Deep read switch is `[integrations.deep] automatic`, which sets `word.Rules.automatic_deep`;
    unset keeps today's behaviour, and production sets it `false`.
- **Pi** goes to the newest release at build time that knows the GPT-6.2 models: 1.6.0 (2026-11-20) today.
- **The folded re-trigger fix goes with the jobs worker in P3:** an owner's Deep request bumps a running Process job
  only when the job's steps are affected.
- **Shipping.** Each stage is a hard-class PR with drive's review loop; Opus implements and Sol reviews. Stages merge
  and deploy back to back. While the loops move (P2, P3), no other backend PR is open.

## Structure

```text
submitter's write unit ── store.work.submit(key, kind, scope, not_before) + NOTIFY ──▶ commit
                                                                                     │
WorkGate (one per process) ◀── LISTEN / next not_before / 30 s poll ─────────────────┘
  admit: one per kind (per integration for deliveries); run lock still inside nightly and chunks
  lease ─▶ Worker sets context {item, lease, ceiling, deadline, attribution}
             └─▶ Service ──▶ db.write(...)  fenced: item row FOR SHARE where token matches
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

## Risks

- **R1. Five loops replaced, and the nightly breaks.** Handled by a strict sandbox nightly on the same window before
  and after (V3), each kind run end to end on the sandbox (V2), an additive schema so the previous image can be
  redeployed until P7, and one checked weekday night after the last deploy (V9).
- **R2. The fence's row lock makes heartbeats or takeovers wait.** Every write unit of an item holds its row
  `FOR SHARE`, and a renewal updates it. Units are short since A1, the slow-unit warning surfaces a long one, and a
  heartbeat every third of the lease makes a wait of seconds harmless.
- **R3. One Model gate's limits slow the owner's work during a nightly.** The owner's passes now share per-provider
  limits with the nightly, as the spec intends (no provider's limit doubled). If an owner pass waits noticeably, the
  fix is A3's lanes and is noted there.
- **R4. Batch recovery credits answers to the wrong place.** Recovery runs once per process; an answer keeps its
  original run and item, and the bookkeeping runs under the system attribution. Tested with a batch left open by a
  killed worker (V4).
- **R5. The deep update changes how Deep reads are written.** Pi 1.0 to 1.6 and the model 6.1 to 6.2. Deep reads are
  off, and switching them back on needs its own shaping (backlog); the finder is the first user of the new versions,
  covered by V7.

## Stages

| | Stage | Shows before the next starts |
|---|---|---|
| **P1** | **The gate.** The migration; `WorkGate` with submit (a Repository call), keys (a scheduled one holds for good), `not_before`, admission per kind or per integration, leases and heartbeats, take-back, the fence in write units, LISTEN and the fallback poll; the `workers` rows; a Worker runner; the attribution context, not yet checked. Nothing uses it yet. | The gate's tests (V1); CI green. |
| **P2** | **Passes and deliveries.** Full-text and Deep passes become items, submitted where settling queues them and by a startup sweep for those already queued. Dispatch becomes delivery items per integration. Their loops, wake events, ticks and the Deep and dispatch session locks go. | V2 for these kinds on the sandbox, deliveries to a test webhook included; then merge and deploy. |
| **P3** | **Scheduler, nightly, jobs.** The scheduler submits fires within their catch-up window, and its lock goes. The nightly is one item under the run lock. Job chunks are items, the job's run spanning them and its ceiling covering the job. Liveness comes from leases, and the presence session goes. The re-trigger fix. | V2 and V3 on the sandbox; then merge and deploy. |
| **P4** | **One Model gate.** Providers, limits and batchers per (provider, deadline) in one gate per process; ceiling, deadline and journal from the work context; recovery once at startup; `work_id` on calls; `eval ladders` through it. | V4; V3 again; then merge and deploy. |
| **P5** | **CLI and attribution.** The model and pipeline commands submit and wait; evaluations carry their inputs and stream their results to their files; Ctrl-C asks whether to cancel or detach, and `litwatch work follow ID` picks an item back up. The API, MCP, monitor and CLI set request attribution, and the check turns strict. | V5, V6; then merge and deploy. |
| **P6** | **Release links.** The finder's endpoint and prompt in deep; `release_links`; the triggers (an analysis, a new version, the owner's action); placeholder re-checks; the paper's page, the API and MCP; the automatic Deep read switch. | V7; then merge and deploy with deep's image. |
| **P7** | **Cleanup.** The old liveness columns and the pass's own lease outside the nightly dropped; the held-across-calls list down to the run lock; the spec's status updated. | CI green; merge and deploy; then the night (V9). |
| **T1** ∥ | **Deep track:** the shared contracts (D2), one LaTeX parser (D3), Pi to the newest release, `gpt-6.2-sol` for the Deep read (high effort), the finder's own model setting (`gpt-6.2-sol`, medium). Starts with P1; P6 needs it. | V8; merged and deployed with litwatch's image as it lands. |
| **T2** ∥ | **OpenAI adapter:** the Responses API with structured output, the Batch API behind the batcher's interface, prices and bounds. Built against today's provider interface from P1 on; it moves into the gate in P4. | V4's adapter part; merged whenever ready. |

## Verification

| | Claim | Evidence |
|---|---|---|
| **V1** | A submission commits with its unit or not at all. A second submission with an open key is the same item, and a scheduled key holds after its item ends. Items wait for their `not_before`. Admission is one per kind or per integration. An expired lease is taken back and the item tried again. A write under a lost lease fails, and a takeover waits for an open unit. A worker that exits while its last bookkeeping write fails leaves its item to be tried again. | pytest on real Postgres (`tests/test_work_gate.py`), one test per sentence; the fence and the takeover with two connections under a timeout. |
| **V2** | Each kind runs as a Worker as before: full-text passes, Deep passes, deliveries, the publications lookup, the nightly, Process, re-run, replay and Write again. A nightly blocked by the run lock stays pending, no chunk starts while a nightly is due or running, and a pass the nightly worked inline leaves no item to run. | pytest per kind. On the sandbox: a Process job, a re-run under a $0.50 ceiling, a replay, a Write again, a Deep request to a dev deep instance and deliveries to a test webhook, their run records and results read back. |
| **V3** | The nightly runs as before. | On a sandbox built from the archive's papers, `litwatch nightly --no-fetch --limit 25` with `LITWATCH_STRICT_GATES=1`, at the base and after P3, P4 and P5: the run records' counts match, and no check raises. At most $5 a run. |
| **V4** | One Model gate per process. A ceiling holds across a job's chunks. Calls with the same provider and deadline share batches. A batch left by a killed worker is recovered once and credited to its original call. The OpenAI adapter's normal and batched replies parse, and their cost lands in the journal. | pytest with fake providers (`tests/fakes.py`) and faked OpenAI SDK objects; one real `litwatch ping` through OpenAI and one real Batch of 2 requests. |
| **V5** | Every unit and model call is attributed. | The suite in strict mode; V3's strict nightly logs no attribution error; the API tests and the stack smoke test pass. |
| **V6** | The CLI submits and waits, refuses without a worker, survives a restart of `serve` while it waits, and an evaluation resumes from its files. | pytest with a worker in process. On the sandbox, with `serve` on its own port: `nightly --limit 5`; `eval classify` on 10 labels, stopped and resumed; `ping`; the same with `serve` stopped, which must refuse. |
| **V7** | Release links are found with the right access states and shown; placeholders are re-checked; automatic Deep reads stay off. | `npm test` with a fake model. A real find on about 10 sandbox papers through a dev deep instance, covering public, gated, placeholder and none found, which the owner looks over. Re-checks against a real placeholder repository. The paper's page walked at 390 and 1440 px, dark and light. MCP `get_paper` shows the links. |
| **V8** | Python and deep share generated contracts, one outline parser serves both, and deep runs on the newer Pi and model. | CI fails on a stale generated contract (shown once in the PR). Parser tests over the papers both parsers covered. `npm test`, `npm run typecheck`, the stack smoke test. |
| **V9** | Production holds. | The first weekday night after the last deploy: its run record against the week's; the logs searched for attribution, nesting, fence, lease-lost, take-back and pool errors; no stuck items; integrations caught up; release links found for that night's papers. |

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

## Evidence

Filled in as stages land.
