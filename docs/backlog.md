# Backlog

Everything specified or agreed but not built yet, in the order it should be picked up. An item leaves this list
when it is merged; one that needs shaping first says so. Add to this list rather than leaving a gap only in a spec, a
plan's notes or a `Later` placeholder in the web app.

v1's unbuilt parts (from an audit of `6ace837` on 2026-10-22, checked against the code) and the codebase review of
2026-10-21 ([`notes/codebase-review-2026-10-21.md`](notes/codebase-review-2026-10-21.md), "review" below) are folded
into the rework's milestones, each into the one that rebuilds its area. A milestone is done only once the items under
it are merged. A note leaves `docs/notes/` once every item it holds is merged.

## Now

**The Deep read**, replacing the Deep card: method and results at close to the paper's detail, a claims ledger,
the paper's figures, rebuilt tables and diagrams, sections as rungs of the paper page; written by the `deep` agent
with a `render` tool, checks in `submit` and a reviewer. Also: written only for the topics the paper is graded in,
which ends the CoT extensions on unrelated papers; Notion's short form; Zotero drops the card. Shaped 2026-11-09 in
[`specs/deep-read.md`](specs/deep-read.md), planned in [`plans/deep-read.md`](plans/deep-read.md) (P1–P5; P1 merged
in #143). Afterwards: a Deep quality harness (`litwatch eval deep`, a judge from another lab), and the prose rules
shared between `writing.py` and the deep prompt instead of copied.

## The rework

The backend brought to the roles and gates of [`specs/architecture.md`](specs/architecture.md), one milestone at a
time. A0, the role checks and the baseline, was merged on 2026-11-10
([`plans/a0-rules-baseline.md`](plans/a0-rules-baseline.md)); A1 starts once the Deep read has landed. Each
milestone's shape goes in `specs/architecture/`, its plan in `plans/`.

### A1. Database gate

Shape, blueprint, drive. Inputs: the measurement of where work waits (2026-11-09/10).

- **Settling a paper made cheap** (review, project 1): one taxonomy snapshot per transaction, current verdicts only,
  each paper settled once.
- **The SQL outside `store`** (review, project 4): the 31 places listed in `tests/test_sql_home.py`.
- **Tests for `change` with several papers** (review, project 7): overlapping sets in reverse order across two
  connections, rollback when a later paper fails; shared test helpers into support modules.
- **`/summary` reworked** (review): spend summed once, narrow reads instead of full Settings data; with it:
  - **the sidebar's count for today:** the count beside a followed topic with a digest today, highlighted for Focus
    (DESIGN.md, "Shell"); `/api/summary` carries badges only;
  - **today in the schedule's zone:** the Digest's defaults (today's Day, the last complete Week, History's pinned
    week) use the browser's date; the summary should carry today's date
    ([`notes/schedule-zone-today.md`](notes/schedule-zone-today.md)).
- **Calibrate reads each topic's pool once** (review).
- **Ratings on the generated text:** ✓/✗ on a Gist and a Brief, from Calibrate and from the paper's ladder, stored
  against the exact text rated (feedback.md, data.md's `text_ratings`). No table, endpoint or control yet.
- **Smaller read gaps:**
  - unclassified papers listed on Papers, not only counted (the API can return them);
  - code and weights links from the analysis, not only from a Deep card's URL;
  - which source set the owner's word on a topic, from the API instead of guessed by the UI (`whoseFit`);
  - typed response models for `/quality`, `/runs` and `/calibrate` (review, project 3).
- **Intake stops rewriting unchanged rows** on its three-day overlap (review, project 8).
- **The role checks' listed violations in its area:** `store` imports `events` (`store/digests.py`, `store/papers.py`
  emit events) and `llm` (`store/calls.py` uses `llm.types.Usage`), listed in `pyproject.toml`.
- **Drops the owner marked** in [`notes/still-used.md`](notes/still-used.md): the live-label `provisional` handling
  and the revision-1 fallback (`papers.py`, `row.get("revision") or 1`).

### A2. Work gate

Shape, blueprint, drive.

- **The OpenAI adapter** in the Model gate, for the owner's OpenAI credit; which roles use it is decided by an
  evaluation once it is in.
- **Python and deep share their contracts** instead of declaring each other's types by hand; the two LaTeX outline
  parsers become one (review, project 3).
- **A first Deep read while the paper's Process job runs** marks the job as asked again by the owner, so it runs
  once more over what is due when it ends: harmless but the paper shows Processing longer. Narrow the trigger to
  owner changes that affect the job's steps (Codex on #127).

### A3. Lanes and the Orchestrator

Shape, blueprint, drive.

- **The taxonomy write API:** edit a class's name, description and threshold in place, with Save and the re-run
  offer (DESIGN.md, "Taxonomy"); Import YAML from Settings (only the CLI imports now); move a followed topic to
  another class (the topic save endpoint binds it to the URL's class). Re-runs after a class edit (classification,
  then grading) come with it. Placeholder: `web/src/routes/settings/taxonomy.tsx`.
- **Correcting a classified paper:** Calibrate's "Classes right?" and adding or removing a paper's class membership
  (feedback.md). Owner memberships are stored already, but classify-by-hand only accepts papers awaiting
  classification. Placeholder: `web/src/routes/settings/calibrate.tsx`. The paper page's class and text correction
  forms get Needs you's safeguards with it (review, project 8).
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

- **N9, the release** ([milestones.md](specs/v1/milestones.md)): `v1.0.0`, install docs, repository clean-up,
  `evals/` and homelab specifics out of the repository (the image ships `evals/labels/` for Run replay: they move
  with the code that reads them, `quality.replay.LABELS`). After A6, which rewrites the install docs and `deploy/`.
- **Backfill** of the archive's older papers (milestones.md): a local decision-style model over several nights, then
  the stronger graders on what it surfaces. Needs A3's lanes and A6's catalog in Postgres.

## Outside the rework

Built between milestones, on whatever structure exists then.

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
- **A0's small fixes left three follow-ups:**
  - `mcp_types`, `starlette` and `pymupdf` are imported but not declared; they arrive through other packages;
  - two more tests in `tests/test_batches.py` share the race #154 fixed (the batched night, the digest batch count):
    each call's journal rows can outlast the batcher's quiet, so the calls split into two batches;
  - `web/src/deepread/DeepRead.test.tsx`, "shows a table cell's note on hover…", fails now and then under the full
    suite and passes alone.

Smaller deferred follow-ups, each owned by its note: [`notes/deep-session-end.md`](notes/deep-session-end.md),
[`notes/updated-papers-follow-ups.md`](notes/updated-papers-follow-ups.md).
