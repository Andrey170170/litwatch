# Backlog

Everything specified or agreed but not built yet, in the order it should be picked up. An item leaves this list
when it is merged; one that needs shaping first says so. Add to this list rather than leaving a gap only in a spec, a
plan's notes or a `Later` placeholder in the web app.

v1's unbuilt parts (from an audit of `6ace837` on 2026-10-22, checked against the code) and the codebase review of
2026-10-21 ([`notes/codebase-review-2026-10-21.md`](notes/codebase-review-2026-10-21.md), "review" below) are folded
into the rework's milestones, each into the one that rebuilds its area. A milestone is done only once the items under
it are merged. A note leaves `docs/notes/` once every item it holds is merged.

## Now

The rework's A2, below: shaped ([`specs/architecture/a2-work-gate.md`](specs/architecture/a2-work-gate.md)) and planned
([`plans/a2-work-gate.md`](plans/a2-work-gate.md)); building.

## The rework

The backend brought to the roles and gates of [`specs/architecture.md`](specs/architecture.md), one milestone at a
time. A0, the role checks and the baseline, was merged on 2026-11-10
([`plans/a0-rules-baseline.md`](plans/a0-rules-baseline.md)). A1, the Database gate, was built and deployed by
2026-11-21 ([`plans/a1-database-gate.md`](plans/a1-database-gate.md)); A2 is next. Each milestone's shape goes in
`specs/architecture/`, its plan in `plans/`.

### A2. Work gate

Shaped: [`specs/architecture/a2-work-gate.md`](specs/architecture/a2-work-gate.md). Planned:
[`plans/a2-work-gate.md`](plans/a2-work-gate.md). Drive.

- **The OpenAI adapter** in the Model gate, for the owner's OpenAI credit: normal calls and the Batch API. Which
  roles use it is decided by an evaluation once it is in.
- **Release links:** code and weights links found by a narrow agent on the deep service, with their access state,
  shown on the paper's page and in the API; placeholders re-checked weekly for four weeks; the deep service running
  with automatic Deep reads off (the spec, "Release links"). Replaces the code and weights item that was outside the
  rework.
- **Python and deep share their contracts** instead of declaring each other's types by hand; the two LaTeX outline
  parsers become one (review, project 3).
- **The deep service brought up to date:** Pi to the newest version that knows the GPT-6.2 models, and the Deep
  read's default model to `gpt-6.2-sol`.
- **A first Deep read while the paper's Process job runs** marks the job as asked again by the owner, so it runs
  once more over what is due when it ends: harmless but the paper shows Processing longer. Narrow the trigger to
  owner changes that affect the job's steps (Codex on #127).
- **Leases replace the presence session.** A1's P4 made each process's Database gate hold a presence connection
  outside its pool, whose `(pid, backend_start)` (`DatabaseGate.identity`) stands for the process in run records and
  job claims (`runs.start`, `ALIVE`, the job SQL, `take_back`); a presence that drops is opened again under a new
  identity, and the worker closes its runs and claims afresh. The Work gate's work records replace it with leases
  and heartbeats, and the presence connection goes (decided 2026-11-18, `docs/plans/a1-database-gate.md`, "Choices
  made without asking").
- **Work fenced when its ownership is lost.** Since A1's P4, what owns a piece of work lives on side sessions (the
  session locks' connections, the presence session) that the pooled work never watches (the plan's notes, "A2: work
  is not fenced when its ownership is lost"). Until A2:
  - if only a lock's or the presence's session drops, or Postgres restarts while litwatch keeps running, the work
    goes on to the end of its block, and a peer may take the lock or the job and run the same work beside it;
  - a nightly can look dead while it still works, so background Deep work may start beside it;
  - a run whose last close fails (its pool backend terminated, or no pool connection within the timeout) stays
    alive and keeps queued jobs waiting until the process restarts;
  - a pooled connection lost in the middle of a job's chunk, or a Postgres restart that recovers within the pool's
    wait, ends the job `failed`, for the owner to retry: before P4, ending it failed on the same dead connection and
    the worker took the job back. A2's worker recovery leaves such a job to be tried again.

  A2 fences work once its lease is lost; its acceptance includes a worker that exits while its last bookkeeping
  write fails. The run lock's case stays open through A3, which removes that lock.

### Model gate hardening

Found in A2 P4's last review round and fix checks, each reachable only in unusual cases, and deferred once its review cap was reached.

- **Shutdown waits on a blocked journal write.** `Gateway._journalled` (`llm/gateway.py`) waits through every
  cancellation with no bound, so a write blocked inside Postgres (on a lock another transaction holds) holds up
  `serve`'s shutdown until that lock is released. Bound each attempt and the whole drain, then log any write left
  unfinished.
- **A batch's provider id can go unwritten at shutdown.** The provider accepts a batch, the first write of its id
  (`Batcher._sent`, `CallJournal.batched`, `llm/batches.py`) fails, and the gate closes during the retry backoff: the
  row keeps a null batch id and cost, startup recovers nothing (`ModelGate.recover`), and the same request is sent and
  paid for again. Let post-send batch writes finish within the bounded drain above, while provider polling stays
  cancellable (`Batcher.close`).
- **A reservation outlives a cancelled call.** A call cancelled after its row commits and before it is sent
  (`Gateway._complete`, `opened`) leaves that row's reservation (`inference_calls.reserved`) counted, so the job's
  ceiling is overcounted by that one reservation: a conservative error.
- **The cleanup that removes this class.** Several places decide whether a batched request's answer is already known
  or paid for: `CallJournal.in_batch`, `Gateway._batched` and its cache recheck in `_ask`, and `Batcher.attach` and
  `_taken`. One owner per batch in the gate, holding its waiters and its final result in one map, would take that
  decision in one place.

### A3. Lanes and the Orchestrator

Shape, blueprint, drive.

- **Background Deep work beside a due nightly.** Since A2's P3, background Deep work (an automatic pass, a re-run's)
  waits for the nightly once its fire is an item, but not in the moment between the fire falling due and the
  scheduler submitting it (a retried submission widens it). A Deep pass started then holds the one `deep` admission,
  up to an hour, beside that nightly and delays its Deep reads. Deep reads are off in production. Lanes give the
  nightly its own share (found in A2 P3's review, and by Codex on #188).
- **The taxonomy write API:** edit a class's name, description and threshold in place, with Save and the re-run
  offer (DESIGN.md, "Taxonomy"); Import YAML from Settings (only the CLI imports now); move a followed topic to
  another class (the topic save endpoint binds it to the URL's class). Re-runs after a class edit (classification,
  then grading) come with it. Placeholder: `web/src/routes/settings/taxonomy.tsx`.
- **Correcting a classified paper:** Calibrate's "Classes right?" and adding or removing a paper's class membership
  (feedback.md). Owner memberships are stored already, but classify-by-hand only accepts papers awaiting
  classification. Placeholder: `web/src/routes/settings/calibrate.tsx`. The paper page's class and text correction
  forms get Needs you's safeguards with it (review, project 8).
- **Ratings on the generated text:** ✓/✗ on a Gist and a Brief, from Calibrate and from the paper's ladder, stored
  against the exact text rated (feedback.md, data.md's `text_ratings`). No table, endpoint or control yet;
  the control needs a UI decision. Moved here from A1 (2026-11-17), beside Calibrate's corrections.
- **The topic editor says a re-run of its version was not run** ("Version 4 has a re-run not run ›"), an open
  question from the settings sketch.

### A4. Telemetry and the activity log

Shape (with sketches), blueprint, drive.

- **The Jobs page as the log of all work,** replacing what the All jobs tab shows now.
- **An integration's events:** the per-integration history (time, event, paper, result; all, failed or waiting)
  with Retry and Skip (DESIGN.md, "Integrations"), as a view of its delivery work items. Placeholder:
  `web/src/routes/settings/integrations.tsx`.
- **Misses: Resolved, and a record that keeps its diagnosis.** "Resolved" on a miss needs a write (DESIGN.md,
  "Misses"). The miss record keeps each stage's cells as they were at the report; today the trace is computed from
  current data, so a re-grade or a taxonomy edit rewrites the diagnosis.
- **The charge an error reports is journalled** (OpenRouter bills some completions with no choices): the month's
  spend and a re-run's spend over its runs miss it today (docs/plans/jobs.md, P2's known limit).
- **Estimates for roles whose history is all batched** (`field_grade`, `brief`): `store.calls.mean_cost` leaves
  batch calls out, so a Field topic's re-run offer has no estimate. Twice the batch mean would do.
- **Evals as checks** (review, project 6): maintained inputs in `evals/` apart from historical reports, then a
  performance evaluation CI can run (API latency on the main pages and search, bundle size, render counts).
- **The evaluation harnesses resume past a half-written last line** (review, project 8).

### A5. Fetch, Compute and Files gates

Blueprint each; shape for Files.

- **Zotero's five GETs per unchanged item** go, with its client moving onto the Fetch gate (review, project 8),
  unless "Save to Zotero" (below) replaces the integration first.

### A6. The archive folded in

Shape, blueprint, drive, with a switch plan. Everything in
[`notes/archive-fold-and-search.md`](notes/archive-fold-and-search.md), Project 1, including:

- **old-style ids with uppercase letters** (`math.AG/0601001`) fail on PDF, text and source today;
- `docs/ARCHIVE-INTEGRATION.md` and `docs/ARCHIVE-STAGE2.md` removed;
- release CI checking what PR CI checks (review, project 8).

### A7. Search rebuilt

Its own project on top of A6: the labelled query set and goals first, then work until they are met
([`notes/archive-fold-and-search.md`](notes/archive-fold-and-search.md), Project 2;
[`notes/pg-search-spike.md`](notes/pg-search-spike.md)).

### After the fold

- **Work with more than one writer.** A2 is built for one `serve` ([`plans/a2-work-gate.md`](plans/a2-work-gate.md),
  D4). Before a second process serves work, close the races it leaves, found in A2's reviews:
  - a nightly being taken back by one gate loses its place to a job chunk another gate admits (`SKIP LOCKED`);
  - a progress write whose commit acknowledgement is lost counts its papers twice on the retry (Process, replay,
    a re-run's text passes: each counts from where its chunk began, `ModelRun.chunk_began`);
  - the same tallies live in the Worker's memory between a committed result and the chunk's progress write, so a
    restart in between leaves a committed result out of the job's counts (a re-run's Brief, a replay result whose
    acknowledgement is lost). Counts only; one fix closes both: derive a job's progress from its committed results.

- **N9, the release** ([milestones.md](specs/v1/milestones.md)): `v1.0.0`, install docs, repository clean-up,
  `evals/` and homelab specifics out of the repository (the image ships `evals/labels/` for Run replay: they move
  with the code that reads them, `quality.replay.LABELS`). After A6, which rewrites the install docs and `deploy/`.
- **Backfill** of the archive's older papers (milestones.md): a local decision-style model over several nights, then
  the stronger graders on what it surfaces. Needs A3's lanes and A6's catalog in Postgres.

## Outside the rework

Built between milestones, on whatever structure exists then.

- **Deep reads back on**, once models write well enough without the agent's heavy tooling. The Deep read
  ([`specs/deep-read.md`](specs/deep-read.md)) is built (P1 to P6) and off since 2026-11-15: the owner's read of P6
  found it still not well written. Needs shaping first: what to try (a newer model, a lighter setup) and how to
  judge it before switching on. To switch on: build the deep image from litwatch's commit, then enable the `deep`
  service and `[integrations.deep]`; settling queues automatic reads only while that is configured.
  Once A2 is in, the deep service also runs the release-link finder, and automatic reads have a switch of their own
  (A2's spec, "Release links"), which switching on sets too. Then: a Deep quality harness (`litwatch eval deep`, a judge from another lab), and the
  prose rules shared between `writing.py` and the deep prompt instead of copied.

- **Release links in the digests and the Notion export,** once A2's finder has run long enough to show how often
  links are found (A2's spec, "Not in A2"). The Notion export's contract changes with it, so the PI's view is
  checked first.

- **Frontend polish and speed** (review, project 2): elements jumping when a topic tab or paper loads the first
  time; windowed rendering with scroll restoration and paper order kept; lean shelf pages; mutations with their own
  effects; integration Retry's stacked refreshes; Settings split out of the main chunk.
- **Needs you keeps splits until they are labelled.** Splits are listed only from last night's run
  (`store/quality.py`, `splits_window`), so about 30 a night leave the list unlabelled after one day, their more
  inclusive Fit standing. Agreed direction (2026-10-23): splits out of the count, waiting until labelled, and asked
  on the paper's page and in Calibrate. *Needs shaping.*
- **Save to Zotero,** in place of syncing grades to collections: file a paper into the owner's own collections with
  full metadata; shares go share → Process → save, and a shared paper litwatch graded low everywhere counts as a
  likely miss. *Needs shaping* (which collections, WebDAV PDFs, paperflow).
- **Removing 0.14 and the first v1 deployment** (their stacks and containers; the dumps in `~/backups/` stay). Needs
  the owner's go-ahead.
- **A Calibrate session whose drawn topic left Focus:** answering it is a 422 now. *Needs a decision* on what an
  answer there means.
- **The Deep read's writer corrects a PDF's parts.** A PDF's bibliography and appendix are found from its headings
  (`pdfParts`), which odd naming or layout defeats: a contents list whose entries sit at the paper's own heading level
  still starts the cuts, losing the main text from the first message (P5, #164). A missed cut costs only tokens and
  makes the length check lax, since the rest stays readable in the session. Idea (2026-11-10): the writer may move a
  part's border when the parsing is wrong, and the length check stays measured on code's cuts, or on the reviewer's,
  so the writer does not set its own bar. Wait for a PDF-only read whose parts came out wrong. *Needs shaping.*
- **A cleared publication record reaches the exports.** Clearing a paper's record emits no event, so Notion's Venue
  and Zotero's "Published" line keep the old record until the paper's next event or Notion's reconcile. A write that
  lands while an export reads the paper is missed the same way (A1 P5's review). An event on clearing closes both.
- **A0's small fixes left three follow-ups:**
  - `mcp_types`, `starlette` and `pymupdf` are imported but not declared; they arrive through other packages;
  - two more tests in `tests/test_batches.py` share the race #154 fixed (the batched night, the digest batch count):
    each call's journal rows can outlast the batcher's quiet, so the calls split into two batches;
  - `web/src/deepread/DeepRead.test.tsx`, "shows a table cell's note on hover…", fails now and then under the full
    suite and passes alone.

Smaller deferred follow-ups, each owned by its note: [`notes/deep-session-end.md`](notes/deep-session-end.md),
[`notes/updated-papers-follow-ups.md`](notes/updated-papers-follow-ups.md).
