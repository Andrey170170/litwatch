# A2. Work gate

Status (2026-11-23): **shaped, not planned.** This is milestone A2 of [the architecture](../architecture.md): every
background loop becomes a Worker over one Work gate, one Model gate serves the whole process, and every gate call
knows what it serves. It also builds one new kind of work, the release-link finder, on the gate. Decisions from the
shaping talk of 2026-11-23 (Q1–Q15).

## Why

A2 changes almost nothing the owner sees. It is groundwork for A3 and A4, plus one feature:

- **Five loops share the service's process** (full-text, Deep, scheduler, jobs worker, dispatch). Each has its own
  tick, wake event and lock, and none leaves a record of what it did. A3's lanes and A4's log of all work need one
  place where work is admitted and recorded.
- **What owns a piece of work lives on side sessions.** Since A1's P4, a process is known by its gate's presence
  session, and runs and job claims are taken back when that session ends. Nothing fences work whose owner is lost
  (backlog, "Work fenced when its ownership is lost"), and the presence session was always meant to be temporary.
- **Each run builds its own model gateway,** so two runs at once double a provider's limits, and CLI commands that
  call models do it from their own process.
- **The owner's OpenAI credit goes unused:** litwatch reaches OpenAI's models only through OpenRouter.
- **Code and weights links are missing** since Deep reads went off on 2026-11-15. The analysis records only whether a
  paper releases code or weights, never where.

## Work items

**A work item is one attempt at one natural unit of work** (Q2):

- the nightly, one item running its steps inside itself as today, its text passes included (A3 splits it into child
  work);
- one chunk of a job (Process, re-run, replay, Write again);
- one full-text pass, one Deep pass, one release-link find or re-check;
- one batch of events for one integration;
- the publications lookup;
- a CLI command's work (below).

**Work records are a table of their own,** beside `runs` and `jobs` (Q1). A record is written before anything runs,
with its kind, scope, timings, outcome, cost and lease. `runs` and `jobs` keep their meaning and their API: a run is
what one work item records (a run links to its item), and a job is what the owner asked for, made of the items that
work it. The owner-facing job states (offered, stopped, tonight) stay product states on `jobs`. Neither table tracks
liveness any more: a run or job is alive while its item's lease is. Whether `runs` folds into work records is A4's
call, when the Jobs page becomes the log of all work.

**What a paper wants stays where it is.** The pass tables still say which passes are due, and a work item is one
attempt at one of them. The code that queues a pass submits its work item in the same write unit, so the data and the
work it calls for commit together, as an event does with its data. The wake events and the 30-second ticks go.

**Duplicates are dropped by a key.** A submission carries a key, such as the paper and pass, or the nightly and the
fire time it serves (`nightly@2026-11-24T02:00`). A second submission with a key that is still open is the same item.
The scheduler stops being a loop that runs work: at each fire time it submits, and the key replaces the schedule-kind
lock.

**Admission is one at a time per kind** (Q3), the concurrency of today's loops. The run lock still decides between
the nightly and job chunks, and no chunk starts while a nightly is due or running. A3 replaces all of this with lanes.

## Leases

**A worker holds its item by a lease,** renewed on a heartbeat. An item whose lease expires (its worker died, or lost
the database) is taken back and tried again by any worker, the same process after a restart included. That replaces
the presence session, which goes, with the Deep worker's, dispatch's and the schedule-kind session locks. The
held-across-calls list keeps only the run lock, which A3 removes.

**Work whose lease is lost is fenced** (Q7). Every write unit opened for a work item confirms, inside its own
transaction, that the item's current lease is still the one the worker holds, and fails if not, so no write from a
lost owner commits after a peer takes the item. A failed heartbeat also cancels the item's task, so the work stops
rather than running on to the end of its block. A pooled connection lost in the middle of an item leaves the item to
be tried again rather than ending it failed. This closes the backlog's cases except the run lock's, which stays open
through A3.

## The Model gate

**One per process** (Q8). It holds the providers, each provider's limits and the batchers; nothing builds a gateway
per run any more. What differed between runs comes from the work context the Worker sets when it takes an item:

- the item's attribution, which the call log records with each call;
- its ceiling (a re-run's, a replay's), which every attempt reserves against as today;
- its batch deadline (the nightly's). Batchers are kept per provider and deadline, so calls of several items with the
  same deadline go in one provider batch, which A3 needs.

Pipeline code keeps calling `ask(role, request)` and `decide(role, request)`.

**The OpenAI adapter** (Q5) reaches OpenAI directly, on the owner's credit: the Responses API with structured output
for normal calls, and the Batch API for roles that batch, as Anthropic's Message Batches do. Its prices and price
bounds come with it, so a ceiling covers it. Which roles move to it is decided by an evaluation once it is in, not in
A2. Flex processing waits until an evaluation asks for it.

## Attribution

Every Database-gate unit, reads included, and every Model-gate call knows what it serves (Q9): a work item, set by
the Worker; an API request, set by the API, MCP and the monitor for everything they do; or a named system attribution
for startup work (migrations) and the gates' own bookkeeping. A gate call with none raises in tests and development
and is logged as an error in production, as the nesting check does. Fetch, Compute and Files join the check when they
arrive in A5.

## The CLI

**Commands that run pipeline work submit it and wait** (Q4): those that call models or fetch for the pipeline (the
nightly, intake, digests, full-text passes, publications, the evaluations, replays). So only `serve` calls models for
litwatch, and its Model gate's limits are the global ones. An evaluation stays a dry run: its item writes the
evaluation tables only.

- With no worker running, the command refuses and says to start `serve`. It never runs the work itself. Tests and
  development run a worker in process.
- Ctrl-C asks whether to cancel the item. Otherwise the command stops waiting, prints the item's id, and a command
  can follow it later.
- Commands that only read or edit (taxonomy import and export, migrations, `openapi`, `ping`) stay Entries that open
  their own units.

## Release links

**A release link** is a link to a paper's released code or weights, with its access state. Since Deep reads went
off, litwatch has no way to find them; the analysis only says whether a paper releases code or weights. A2 builds
the finder on the Work gate, its first kind of work built for the gate rather than ported to it.

- **Trigger** (Q11): every analysed paper whose analysis says it releases code or weights, about 70 a night, and a
  "find links" action on the paper's page.
- **The find** runs on the deep service, as an agent with a narrow task and a small structured answer: start from the
  abstract, the arXiv comments and the paper's text, go to the open web only when those say nothing, and confirm each
  link. Per link: its kind (code or weights), its access state (public, gated, placeholder: a repository with nothing
  but a README, or missing), its license when it is cheap to see, and when it was checked. The answer is the
  contract the shared contracts (below) cover.
- **Its model is a setting of its own** (Q14), by default `gpt-6.2-sol` at medium effort: it is a lookup, not
  writing.
- **Re-checks stay bounded** (Q12). The agent runs once per paper version. A placeholder link is checked again weekly
  for four weeks by a plain request to GitHub's or Hugging Face's API, not by the agent, then left. A paper where
  nothing was found gets the agent again only when a new arXiv version arrives, which is when links usually appear.
  Public links are not re-checked.
- **Where it shows** (Q13): the paper's page and the API, so the MCP server's `get_paper` carries the links. Not the
  digests or the Notion export, whose contract stays as it is.
- **The deep service runs with Deep reads off.** Today one switch enables the deep integration and with it automatic
  Deep reads. A2 separates them: the finder runs while automatic reads stay off.
- **Judged by eye** (Q15). No labelled set first: the owner fixes a wrong link when one shows up.

## Folded in

These are under A2 in [`backlog.md`](../../backlog.md), and A2 is done when they are merged:

- **Python and deep share their contracts** instead of declaring each other's types by hand, and the two LaTeX
  outline parsers become one (review, project 3). The release-link answer is one of the contracts.
- **The deep service brought up to date:** Pi (`@earendil-works/pi-ai` and `pi-agent-core`, at 1.0.0) to the newest
  version that knows the GPT-6.2 models, and the Deep read's default model from `gpt-6.1-sol` to `gpt-6.2-sol`, at
  the high effort it has.
- **A first Deep read while a Process job runs** no longer marks the job as asked again unless the owner's change
  affects the job's steps (Codex on #127). It is rewritten with the job's items anyway.
- **Leases replace the presence session,** and **work is fenced when its ownership is lost** (above).
- **The OpenAI adapter** (above).

## Done when

- every loop is a Worker taking items from the Work gate, and the scheduler only submits; no wake events or ticks
  remain;
- every item has one work record, written before it runs; runs and jobs read liveness from their items' leases, and
  the presence session and the Deep, dispatch and schedule-kind session locks are gone;
- a write from an item whose lease was lost does not commit, and a worker that exits while its last bookkeeping write
  fails leaves its item to be taken back and tried again;
- one Model gate per process, with the OpenAI adapter's normal and batched calls; ceilings and batch deadlines come
  from the work context;
- every unit and model call is attributed, with the check raising in tests and development;
- the CLI's pipeline commands submit and wait, and refuse without a worker;
- release links are found and shown on the paper's page and in the API, with automatic Deep reads still off;
- the folded items are merged.

## Not in A2

- **Lanes, the nightly as an Orchestrator with child work, the run lock's removal, and the taxonomy edit that
  commits and then re-settles.** A3. Until then the nightly is one item under the run lock.
- **Gate events on work records, the normal and debug levels, and the Jobs page as the log of all work.** A4. The
  Jobs page and its API stay as they are.
- **Downloads as work items.** The archive's download queue folds in with the archive, in A6.
- **The Fetch gate.** A5. The new HTTP calls (OpenAI, GitHub, Hugging Face) use today's clients until then.
- **Which roles move to OpenAI.** An evaluation after the adapter is in.
- **Release links in the digests or the Notion export.** Later, once it is clear how often links are found.
- **Details for the plan:** the work record's fields, lease length and heartbeat, the keys, how the work context is
  carried, the CLI's follow command, the stages and their order.
