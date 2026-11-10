# Architecture

Status (2026-11-09): **agreed, not built.** This is the shape of litwatch's backend rework: the rules every part of
the backend follows, and the milestones that bring the code to them. It fixes only what every milestone depends on.
How each gate works inside is settled by that milestone's own shape and plan, under
[`architecture/`](architecture/) and [`../plans/`](../plans/). A milestone may amend this spec, in its own PR,
when what it learns contradicts it.

The rules are taken from Vestnik's v2 design (resource gateways and its role vocabulary) and made for litwatch.
Vestnik's v2 was specified in full and never built; this one ships one milestone at a time, each running in
production before the next starts.

## Why

litwatch works, but its structure grew one feature at a time:

- **Five loops share the API's process** (full-text, Deep passes, scheduler, jobs worker, outbox dispatcher), each
  with its own lease and wake-up, and the archive's download queue is a sixth way work gets admitted. Background work
  leaves no common record, so the Jobs page shows only the jobs the owner asked for.
- **"One big job at a time" is a lock, not a rule.** The nightly holds the run lock for its whole length (median
  42 minutes, p95 5.5 hours, measured 2026-11-09), almost all of it waiting on arXiv and on model batches, and work
  the owner asks for is refused meanwhile. Three other locks are held across network calls: the Deep worker's, the
  dispatcher's and the publications lock.
- **Shared resources have many doors:** eleven HTTP clients across ten modules and three Retry-After
  parsers, eight one-off thread offloads, locks taken in twelve modules, a model gateway per run (two runs at once double a provider's limit).
- **A taxonomy edit re-settles up to 6,777 papers** in one transaction under the exclusive taxonomy lock, and every
  page that loads the taxonomy waits behind it.
- **Failures can't be debugged at the normal level:** a failed model call keeps its error and nothing else. Debug
  mode adds a rejected reply's raw text; the request itself is never kept, only its hash.
- **The archive is a separate package only by history,** reached over HTTP, with its own SQLite catalog and search
  index ([`../notes/archive-fold-and-search.md`](../notes/archive-fold-and-search.md)).

## Roles

Every module has one role, readable from its path. A module may carry a role's name only if it obeys that role's
rules.

| Role | What it is | May use |
|---|---|---|
| **Record** | One domain concept as typed data, with its own invariants (validation, derived fields). No I/O. A Record never calls another Record's behaviour. | other Records as fields |
| **Policy** | A pure, deterministic decision over Records: `grade`, `topic_rarity`, the library rule, what a paper's passes want. No I/O. | Records, smaller Policies |
| **Contract** | Typed data crossing a boundary: API schemas, events, `llm` request and reply shapes, the deep service's jobs. | Records |
| **Prompt** | One `llm` role's pure translation: Records into its request contract, its reply contract back into Records. The Model gate runs it. | Records, Policies, Contracts |
| **Repository** | The SQL for one aggregate, run only inside a Database-gate unit; returns Records. | Records |
| **Gate** | The only door to one shared resource (below). | its own internals; the Database gate for its own bookkeeping |
| **Adapter** | A client for one external service (OAI, DBLP, Zotero, Notion, the deep service): a Contract into an HTTP request through the Fetch gate, the reply back into Records. | Fetch gate, Records, Contracts |
| **Service** | One use case, composed from the roles above and other Services. Owns no resource, has no SQL, holds no lock. | Policies, Prompts, Adapters, Gates, Repositories through Database-gate units, other Services |
| **Worker** | Takes a work item from the Work gate, runs one Service, reports how it ended. No business rules. | Work gate, Services |
| **Orchestrator** | A multi-step flow, such as the nightly: submits its steps as child work and waits on barriers. Does no work and holds no lock itself. | Work gate, Repositories through read units |
| **Entry** | API routes, CLI commands, the MCP server: parse the input, call one Service, shape the output. | Services; read-only units and Repositories for plain reads |

- **Services may call Services** as long as the calls form no cycle.
- **Writes go through a Service. Plain reads need not:** an Entry may open a read-only Database-gate unit and call
  Repositories directly. A read with rules of its own (the summary's spend) gets a Service.
- **Only a Database-gate unit holds a lock,** and no unit stays open while another gate is called.
- Provider adapters (Anthropic, OpenRouter, OpenAI) live inside the Model gate and are not Adapters in this sense.

## Gates

One gate per shared resource. Nothing reaches the resource except through its gate.

| Gate | Owns |
|---|---|
| **Database** | The connection pool, units (short transactions), every lock: named, taken in a fixed order (taxonomy, then papers by id). Read-only units read one snapshot (repeatable read) and take no advisory lock. |
| **Model** | One per process. Provider calls through the provider adapters, per-provider limits, retries, fallbacks, batching, budgets, the call log. |
| **Fetch** | All outgoing HTTP that isn't a model call: per-host pacing (arXiv's shared across processes), timeouts with a total deadline, retries and Retry-After, response size limits. |
| **Compute** | A thread pool for blocking or CPU-heavy work (feed, LaTeX and PDF parsing, hashing large texts), so it never runs on the event loop. |
| **Files** | The paper store on disk (PDFs, LaTeX sources, cached derived text): safe writes and swaps, free-space guard, parallel reads with no global read lock. It neither fetches nor parses. |
| **Work** | Every submission: records it before anything runs, drops duplicates, admits it by lane and capacity, leases it to a worker and takes it back if the worker dies, and keeps one work record per item (what, scope, timings, outcome, cost, links to its model calls and fetches). |

- **Gates are leaves.** No gate calls another, with one exception: any gate may use the Database gate for its own
  bookkeeping (the Work gate's queue, the Model gate's call log and budgets, cross-process limiters), in short units
  of its own.
- **Slow work outside, saving inside.** Fetching, parsing and model calls happen outside Database-gate units; a unit
  only reads or saves.
- **Coordination across processes goes through Postgres,** not Redis or files: arXiv pacing, shared by intake and
  the download worker, is the first case.
- **Every gate call knows what it serves:** a work item, or an API request. The Work gate sets the work item in a
  context variable when a worker takes one; an API request sets its own attribution for everything it does, reads
  included. A request is not a work item: it is attributed, not admitted. A gate call with neither, or a gate
  entered while a Database-gate unit is open, raises in tests and development, and in production is logged as an
  error and carries on. The nesting check arrives with A1, the attribution check with A2.

## Work

- **Async submissions:** the nightly's steps, full-text and Deep passes, digests, re-runs, Write again,
  publications, integration deliveries, downloads. Submitted, and the caller moves on.
- **Sync submissions:** CLI commands that run pipeline work (`litwatch run`, evaluations, replays) submit to the Work
  gate and wait for the result instead of running in their own process. So only the service process calls models,
  and its Model gate's limits are the global ones.
- **Not a submission:** a request's own short write (a label, a thumb, Save). It runs inline as one Database-gate
  unit, attributed to the request; whatever it triggers is submitted.
- **Lanes replace the run lock.** The nightly is one lane of capacity one; the owner's work, passes and deliveries
  run alongside it. Concurrent writes stay safe through the per-paper locks a change takes; spend stays bounded by
  the Model gate's budgets.
- **The nightly is an Orchestrator:** it submits its steps (intake; classification, analysis and grading; full-text
  passes; digests) as child work, waits on barriers, and appears in the log as a parent with children. The Model gate
  gathers many items' calls into one provider batch.
- **A taxonomy edit commits at once,** and re-settling the affected papers is submitted as work in chunks, with
  per-paper locks only. For seconds to a minute, lists show the old standing and the class says it is re-sorting.
- **Events stay as they are:** append-only facts in the outbox, written in the same transaction as their data; the
  contract integrations read, the Notion export's included, does not change. Delivering them becomes work items, one
  batch of events for one integration each, with its result in the log.

## Telemetry

Each gate records its events on the work record it serves: admitted, waited, ran and ended for Work; calls and
retries for Model and Fetch; slow units and lock waits for Database. They are kept in Postgres with a retention
window; stdout logs stay for diagnostics. **The normal level is enough to debug an ordinary failure:** a failed model
call keeps its request and raw reply, a failed fetch its status and an excerpt of the response. Debug mode keeps
everything, and lets a pipeline run a step at a time. The Jobs page becomes a view of these records: a log of all
work, not only the jobs the owner asked for.

## Layout and enforcement

- `core/` holds Records and Policies; `store/` Repositories and migrations; `gates/` the six gates; feature packages
  (`intake`, `classification`, `grading`, `fulltext`, `digests`, `feedback`, `catalog`, `acquisition`, …) their
  Services, Prompts, Adapters and Workers, one module per role (`grading/service.py`, `grading/prompt.py`);
  `api/` and the CLI the Entries.
- Names: the role is in the module's file name; a class carries its role as a suffix (`FetchGate`, `ZoteroAdapter`,
  `DeepWorker`). `manager`, `handler`, `utils` and `helpers` are not names.
- The rules are checked, not only written down, and each check stays simple enough to read at a glance:
  - which role may import which: one `import-linter` contract per role, in `pyproject.toml`;
  - no cycle between Services: one test over the import graph;
  - no raw resource outside its gate (HTTP clients outside Fetch, the Model gate's provider adapters excepted; `to_thread` or executors outside Compute, `pg_advisory`
    outside Database): a search test per gate;
  - the runtime check above.

  Each check starts with a list of today's violations that may only shrink, as `tests/test_sql_home.py` does. New
  code gets no exceptions. A rule that needs a complicated check is probably the wrong rule.

## Milestones

One at a time, in this order. Each is done when its "done when" holds **and** the backlog items folded into it are
merged; [`../backlog.md`](../backlog.md) lists them under each milestone.

| | Milestone | Done when | Process |
|---|---|---|---|
| **A0** | Rules and baseline | this spec merged; the role checks in CI with today's violations listed; what production no longer uses found, so nothing dead is migrated | blueprint |
| **A1** | Database gate | one pool; short units, snapshot reads; locks named, ordered and owned by the gate; the runtime nesting check; settling a paper cheap and done once per paper | shape → blueprint → drive |
| **A2** | Work gate | every loop is a Worker taking items from the Work gate; one work record per item; one Model gate per process, with the OpenAI adapter; work and request attribution in context, with its runtime check; the CLI submits and waits. Behaviour otherwise unchanged | shape → blueprint → drive |
| **A3** | Lanes and the Orchestrator | no run lock; the nightly submits child work; the owner's work runs during it; taxonomy edits commit, then re-settle; deliveries are work items | shape → blueprint → drive |
| **A4** | Telemetry and the activity log | gate events on work records; normal and debug levels; the Jobs page as the log of all work | shape (with sketches) → blueprint → drive |
| **A5** | Fetch, Compute and Files gates | all HTTP, blocking work and paper files behind their gates; the Postgres limiter; the bypass checks with empty lists | blueprint each; shape for Files |
| **A6** | The archive folded in | catalog and downloads in litwatch's Postgres on the ParadeDB image; the download worker a second process from the same image; search as good as today's; everything in compose; one cutover night | shape → blueprint → drive, with a switch plan |
| **A7** | Search rebuilt | a benchmark and its goals set, then work until they are met | its own project |

A1 needs A0; A2 needs A1; A3 and A4 need A2; A5 needs A1; A6 needs A2 and A5; A7 needs A6.

## How it ships

On `main`, as ordinary changes: each milestone's stages are PRs, each running in production before the next. The
violation lists shrink with each one, so `main` is always somewhere between today's structure and this one, and
always working. Product work (the Deep read first, then whatever is outside the rework) goes on between milestones.
The exception is A6's cutover: 3.17M catalog records move from SQLite and the archive's systemd units become compose
services, on one planned night with a check, as v1's switch was.

## Not in this spec

How each gate works inside (pool sizes, lock names, the work record's fields, lanes and capacities, pacing, retention
windows); the grain of work items; the search engine's ranking. Each belongs to its milestone.
