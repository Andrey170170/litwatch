# A2. Work gate

Status (2026-11-27): **built**, P1–P7, T1 and T2, deployed through P6; P7's deploy and the night that checks the
milestone (V9) follow. The build plan, with its evidence, is [`plans/a2-work-gate.md`](../../plans/a2-work-gate.md).
This is milestone A2 of [the architecture](../architecture.md): every
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
with its kind, scope, timings, outcome, cost and lease. `runs` and `jobs` keep their meaning and their API:

- a run still spans what it spans today. A standalone run (the nightly, a pass wave, an evaluation) links to its
  item. A job's run links to every chunk item that works it, and a re-run's or replay's ceiling covers the whole job:
  each chunk reserves against what the job has left;
- a job is what the owner asked for. Its owner-facing states (offered, stopped, tonight, waiting for text between
  chunks) stay product states on `jobs`;
- neither table tracks liveness any more. A lease says what is executing now, and the job row says where a job stands
  between chunks.

Whether `runs` folds into work records is A4's call, when the Jobs page becomes the log of all work.

**What a paper wants stays where it is.** The pass tables still say which passes are due, and a work item is one
attempt at one of them. The pass claim stays the single claim on a pass: the nightly and a re-run's chunk keep
working their text passes inline, a standalone item takes the same claim, and an item whose pass was done or claimed
meanwhile ends with nothing to do.

**Submitting is a write, like an event.** The code that queues a pass writes its submission through the work
Repository in the same write unit, so the data and the work it calls for commit together, as an event does with its
data. That is a Repository call, not a gate call. The Work gate itself admits, leases and takes back items in short
units of its own, and never inside a caller's unit.

**Items wait on time, not on loops.** An item may carry the earliest time it may run: a retry's backoff, an
integration's delivery backoff, a pass still waiting for the archive's text, which is looked at again later as
today. The Work gate wakes when such a time comes or a submission commits. The feature-local loops, their ticks and
their wake events go.

**Duplicates are dropped by a key.** A submission carries a key, such as the paper and pass. A second submission
with a key that is still open is the same item. A scheduled occurrence's key (`nightly@2026-11-24T02:00`) holds for
good, so a fire is submitted once even after it finished, by however many processes or restarts. The scheduler stops
being a loop that runs work: it submits each fire, a missed one too within today's catch-up window, and its key
replaces the schedule-kind lock.

**Admission keeps today's concurrency** (Q3): one item at a time per kind, but four text passes at once as a wave
works them today, and one per integration for deliveries, so a slow integration does not hold up the others. The run lock still decides between the nightly and job chunks: a
nightly that finds the lock taken stays pending rather than failing, no job chunk starts while a nightly is due or
running, and a lease never stands in for the run lock. A3 replaces all of this with lanes.

## Leases

**A worker holds its item by a lease,** renewed on a heartbeat. An item whose lease expires (its worker died, or lost
the database) is taken back and tried again by any worker, the same process after a restart included. That replaces
the presence session, which goes, with the Deep worker's, dispatch's and the schedule-kind session locks. The
held-across-calls list keeps only the run lock, which A3 removes.

**Work whose lease is lost is fenced** (Q7). The Work gate alone moves a lease: taking an item, renewing, taking it
back, ending it. Every write unit opened for an item checks, in its own transaction, that the worker's lease is the
item's current, unexpired one, and holds the item's row so that a takeover waits for the unit to end; so no write
under a lost lease commits after a peer takes the item. A failed heartbeat also cancels the item's task, so the work
stops rather than running on to the end of its block. A pooled connection lost in the middle of an item leaves the
item to be tried again rather than ending it failed. This closes the backlog's cases except the run lock's, which
stays open through A3.

## The Model gate

**One per process** (Q8). It holds the providers, each provider's limits and the batchers; nothing builds a gateway
per run any more. What differed between runs comes from the work context the Worker sets when it takes an item:

- the item's attribution, which the call log records with each call;
- its ceiling (a re-run's, a replay's), which every attempt reserves against as today;
- its batch deadline (the nightly's). Batchers are kept per provider and deadline, so calls of several items with the
  same deadline can share batches, which A3 needs.

Pipeline code keeps calling `ask(role, request)` and `decide(role, request)`.

**The OpenAI adapter** (Q5) reaches OpenAI directly, on the owner's credit: the Responses API with structured output
for normal calls, and the Batch API for roles that batch, as Anthropic's Message Batches do. Its prices and price
bounds come with it, so a ceiling covers it. Which roles move to it is decided by an evaluation once it is in, not in
A2. Flex processing waits until an evaluation asks for it.

## Attribution

Every Database-gate unit, reads included, and every Model-gate call knows what it serves (Q9):

- a work item, set by the Worker;
- a request, set for everything it does by the API, MCP and the monitor, and by each CLI command for its own units,
  its submission and its follow-up reads;
- a named system attribution for startup work (migrations) and the gates' own bookkeeping. A batch read after the
  item that sent it ended is accounted under the gate's system attribution, and each call it answers keeps its own
  item's attribution.

Attribution says whom a unit serves, not what it may do: an item's progress and its end take its lease (above). A gate
call with no attribution raises in tests and development and is logged as an error in production, as the nesting
check does. Fetch, Compute and Files join the check when they arrive in A5.

## The CLI

**Commands that call models or run pipeline work submit it and wait** (Q4): the nightly, intake, digests, full-text
passes, publications, the evaluations, replays and `ping`. So only `serve` calls models for litwatch, and its Model
gate's limits are the global ones.

- With no worker that runs the command's kind (no `serve`, or one with its pipeline workers off), the command refuses
  and says so. It never runs the work itself. Tests and development run a worker in process.
- Ctrl-C asks whether to cancel the item. Otherwise the command stops waiting, prints the item's id, and a command
  can follow it later.
- **An evaluation's submission carries its inputs and candidate settings,** and its results come back to the command,
  which writes the output files and keeps their resume contract as today. Its item writes nothing production's pipeline
  reads: no verdicts, no outbox events, no provider observations. Its charges are real spend, so the month-to-date
  budget counts them.
- **One command runs in the command itself:** `intake archive --here`, for a database no `serve` works from (a fresh
  sandbox's build). It calls no model.
- Commands that only read or edit (taxonomy import and export, migrations, `openapi`) stay Entries that open their
  own units.

## Release links

**A release link** is a link to a paper's released code or weights, with its access state. Since Deep reads went
off, litwatch has no way to find them; the analysis only says whether a paper releases code or weights. A2 builds
the finder on the Work gate, its first kind of work built for the gate rather than ported to it.

- **Trigger** (Q11): a paper whose analysis says it releases code or weights, about 70 a night, when it is analysed
  and again when a new arXiv version arrives; and a "find links" action on the paper's page, which always runs a
  fresh find and shows its result whatever the analysis says. Automatic finds start from the analyses and versions
  recorded once the finder is deployed (P6): a paper analysed before gets none, and the owner's "find links" covers
  it. No backfill is built.
- **The find** runs on the deep service, as an agent with a narrow task and a small structured answer: start from the
  abstract, the arXiv comments and the paper's text, go to the open web only when those say nothing, and confirm each
  link. Per link: its kind (code or weights), its access state (public; gated, behind a request form; placeholder, a
  repository with nothing but a README; missing, a link the paper gives that leads nowhere), its license when it is
  cheap to see, and when it was checked. A paper may also have no link found. The answer is one of the shared
  contracts (below).
- **Its model is a setting of its own** (Q14), by default `gpt-6.2-sol` at medium effort: it is a lookup, not
  writing.
- **Re-checks stay bounded** (Q12). The agent runs once per paper version (per update, for a paper of no known
  version) to an answer, besides the owner's action; a find that failed is tried again by a later trigger, three
  failures at most. A placeholder link is checked again weekly for four weeks by a plain request to GitHub's or
  Hugging Face's API, not by the agent, then left. A paper with no link found gets the agent again only with a new
  arXiv version, which is when links usually appear, with two exceptions: an update to a paper of no known version
  (above), and the owner's "find links". Public links are not re-checked.
- **Where it shows** (Q13): the paper's page and the API, so the MCP server's `get_paper` carries the links. Not the
  digests or the Notion export, whose contract stays as it is.
- **Deep's switches are separated.** Today the deep integration, once configured, both serves the owner's Deep reads
  and queues automatic ones. A2 gives automatic queueing a switch of its own: the finder and the owner's reads use the
  configured deep service, automatic reads stay off here, and an install that has them on keeps them.
- **Judged by eye** (Q15). No labelled set first: the owner reviews the links as they appear and flags wrong ones.

## Folded in

These were under A2 in [`backlog.md`](../../backlog.md), and are merged with it:

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

- every loop is a Worker taking items from the Work gate, and the scheduler only submits; no feature-local loop,
  tick or wake event remains;
- every item has one work record, written before it runs; runs and jobs read liveness from their items' leases, and
  the presence session and the Deep, dispatch and schedule-kind session locks are gone;
- a write under a lost lease does not commit, and a worker that exits while its last bookkeeping write fails leaves
  its item to be taken back and tried again;
- one Model gate per process, with the OpenAI adapter's normal and batched calls; ceilings and batch deadlines come
  from the work context;
- every unit and model call is attributed, with the check raising in tests and development;
- the CLI's model and pipeline commands submit and wait, and refuse without a worker for them;
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
