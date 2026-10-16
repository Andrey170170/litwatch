# Building v1: the new core and the new web app

Approved by the owner on 2026-10-14. This is the reference for implementing the v1 redesign: the order of work, how
it is split into PRs, where it stops for the owner, and how each part is shown to work. What v1 does and why is the
spec, [`docs/specs/v1/`](../specs/v1/README.md), and its web app is [`DESIGN.md`](../specs/v1/DESIGN.md); where this
plan and the spec differ, the spec wins and this plan is corrected. Terms are the glossary's.

## Goal and approach

Build milestones N0–N7 of [`milestones.md`](../specs/v1/milestones.md) on branch `v1`, up to the point where the
switch (N8) can be planned with the owner. Two tracks run side by side once the core has real data to show: the core
(classification, analysis, grading, the full-text queue, digests, the archive in the stack, the restart) and the web
app, rewritten page by page to `DESIGN.md`.

Choices made without asking the owner:

- **The web stack stays** (React, TanStack Router and Query, openapi-fetch, Tailwind 4, Radix primitives). Every
  page and component is rewritten to `DESIGN.md`; the shadcn-styled parts are replaced where they fight the look.
  Fonts are self-hosted through Fontsource (no CDN: the stack is self-contained). Sheets and the drawer use `vaul`;
  swipe-back and stepping are one small pointer-events hook.
- **Old code goes when its input goes** (D8): N2a removes triage, the keyword audit, takes, Watch grading, field
  reviews, issues and everything built on them, and retires the old web app to a shell (D9). No dual paths: nothing
  deployed runs the branch until the restart (D1).
- **The API is reshaped per surface**, as each core milestone lands, with `web/openapi.json` and the client types
  regenerated in the same PR.
- **The taxonomy seed** is `src/litwatch/seeds/taxonomy.yaml`, built from [`taxonomy.md`](../specs/v1/taxonomy.md):
  the classes and the followed topics, in the format Settings imports and exports.
- **Who builds**: the core by the main agent (Opus 5.5); the web app by Fable 5.1 in its own worktree, as in M3.
  Every PR is reviewed locally by GPT-6.1-Sol (medium; high for hard stages), Astra for the archive's authentication
  and anything security-shaped; findings are triaged into discard, fix now and defer.
- **Each PR** follows the normal PR flow into `v1` ([`docs/agents/workflow.md`](../agents/workflow.md)) with its
  evidence by [`docs/agents/verification.md`](../agents/verification.md). The quality harness and the preference set
  are built with the milestones that introduce what they measure: N1 (classification), N2 (Gists), N3 (Briefs), N4
  (digests).

## Order of work

```
archive   A   archive/, the code as one commit
core      N0  tree, topics, evaluation labels
          N1  decide() and the classifier bake-off
          N2a classification, analysis, intake from the archive
          N2b grading, rarity, library
          N2c replay, the two-week run
          N3  full-text queue
          N4  digests
          N5a archive in the stack, storage policy, smoke test
          N5b search relevance, papers outside the collection
          N5c shared flows                                  (after N5b and W2)
          N6  restart
web       W1  shell, look, Papers, topic page, paper page   (starts after N2b)
          W2  Settings
          W3  Digest: Day, Week, History                    (starts after N4)
          W4  phone gestures, PWA, smoke-test pages
owner     after N0: at most 20 disagreements · after N1: the classifier default ·
          after N4: the restart weeks' digests read against 0.14 · W1, W3: "feels like 0.14, only nicer"
```

About 15 PRs. N6 and W4 together are the first build the owner reads; until then the owner reads 0.14, which keeps
taking in new papers and registering them with the archive.

### The PRs

**A · The archive moves in.** The code of `Andrey170170/arxiv-archive` (its package, tests, `pyproject.toml`,
`requirements.lock`) is copied into `archive/` as one squashed commit naming the source commit, with a README for its
place here; the old repository keeps the history and the instance's records (manifests, evidence, the historical
downloader, the runbooks for the owner's server). Its tests run in CI as their own job. Nothing deployed changes: the running archive keeps its own checkout until N5.
AGENTS.md lists `archive/`.

**N0 · Tree, topics and evaluation labels.**
- No migration: the tree and topics stay in the seed file until N2, whose classification step is the first to read
  them; `classes`, `class_versions` and the reshaped `topics` ([data.md](../specs/v1/data.md)) come then, with
  `litwatch taxonomy import` and `export`. N1 reads the tree from the file, as it reads the labels.
- `seeds/taxonomy.yaml` from taxonomy.md: every class with its description, the followed topics with depth, subject,
  rubric (Focus), keywords, colour and monogram. The two CoT topics keep 0.14's subjects, rubrics and keywords,
  reworded from tiers to Fit levels; the Area and Field topics take 0.14's subjects; Circuits and causal analysis and
  Language models take the definitions agreed on 2026-10-14 (taxonomy.md). `litwatch taxonomy check` validates a taxonomy
  file: unique names, at most three levels, every class described, every topic on an existing class, every Focus
  topic with a rubric and no other topic with one.
- The evaluation set as files in `evals/labels/`, never in the live database (decision 55): the owner's first-v1
  labels mapped to Fit (relevant → central, adjacent → adjacent, not relevant → none, the first-v1 value kept in a
  `legacy` column) and the provisional positives; the regression set; 0.14's selection tiers as silver memberships of
  the matching classes (tier 1–2 member, tier 4 not, tier 3 left out); and the silver class sample below. The source
  rows were exported from the first v1's databases on 2026-10-14 before its deployment stopped; 0.14's tiers come
  from the development database's adopted copy of production.
- The silver class sample: a stratified 200-paper draw from 0.14's screened papers with a recorded seed (20 CoT
  positives, 10 owner-labelled papers, 20 positives of each broad selection, 70 papers no selection took), labelled against the tree by Opus 5.5 and GPT-6.1-Sol separately, each
  naming the paper's primary class and its other classes. Agreement is the silver label: the primary class, and each
  class both labs put the paper in, ancestors included (one only one lab did is unsettled and scored neither way); papers whose primary classes
  differ go to the owner, at most 20, on a one-tap page shown in the T3 thread whose answers are pasted back and
  committed. With more than 20, the 20 are drawn with the recorded seed, spread across top-level classes; the rest
  are kept as unresolved, counted in the report and left out of primary-class accuracy. Titles and abstracts are read from the development
  database's copy of 0.14, read only.
- Check: the tree loads and round-trips through YAML; every class has a description; every topic has its subject and
  every Focus topic its rubric; label counts per class and source (owner, 0.14, silver, adjudicated).

**N1 · The decision request and the bake-off.** `Gateway.decide` with the OpenRouter Decisions adapter and the
chat-with-schema adapter; the classification request built from the tree; `litwatch eval classify` over the N0
evaluation set for Luna Decisions, Jev and Haiku 5.5. The report in `evals/` gives, per candidate and per cohort,
never pooled, each metric on the cohorts that carry its label: recall per Focus class at the threshold and with the
band and the keyword check (owner, provisional, regression, 0.14), primary-class accuracy (silver, adjudicated),
per-class precision on the top levels (silver, 0.14), and cost per 1,000 papers, latency and failure rate. The
quality harness starts here, for classification. Owner check: the default.

**N2a · Classification, analysis and intake from the archive.** Four PRs: the teardown (D8, D9); the taxonomy and
topics in the database; classification and analysis as nightly steps; intake from the archive, which runs beside the
other three in its own worktree. Between the teardown and N2b the nightly run is intake, classification and analysis
only.

**N2a, in full.** Classification and analysis as nightly steps with
their records and events (pipeline.md sections 2–3), the uncertain band and the keyword check; the Fit and Weight
definitions in the prompts that judge them; triage, the keyword audit and takes removed. The archive gains a list of
papers by date window and category, and intake a second source that reads it and stores papers as the arXiv source
does; the restart, the sandbox and later the backfill use it. `scripts/refresh-sandbox.sh` rebuilds the sandbox as a
fresh schema plus a window read from the archive.

**N2b · Grading, rarity and the library.** Grading topics from direct memberships, Fit per depth (Focus two labs,
Area with the decision check and escalation, Field one model), the grade table, effective Fit and grade, rarity, the
library rule, budgets; the old Watch grading removed. The reading API for papers, topics and a paper page.

**N2c · Replay and the two-week run.** `eval replay` and `eval report` with the four recall numbers per cohort; the
sandbox's two weeks run through the pipeline (the estimate shown and approved first); the grade table and rarity
offsets tuned on the labels; measured cost per weekday written beside the estimates in pipeline.md. Before the run,
the live archive runs this repository's `archive/` and has run `paper-archive index-dates` once, so `GET /v1/papers`
fills the sandbox (owner, 2026-10-14): its catalog is backed up first, the change only adds the `paper_dates` table,
and 0.14 and v1 registering the same papers stays one record and one download per version, as the archive keys
papers, jobs and files.

**N3 · The full-text queue.** The archive client's urgent requests and text reads, and whatever the archive needs for
them (the arXiv version of the text it serves); Briefs with the overstatement line and the Gist from the full text
(the `gists` table, `gist.written`), Weight re-checks, Deep card passes with each card's usage (tokens, turns, list
cost) recorded with its attempt, the lease, revisions; the "among the collection" section in `deep/`. A disposable archive started per run for
verification. The harness gains Briefs.

**N4 · Digests.** Due, daily and weekly digests per depth with their Gists and picks, quiet days, records and set
asides, the bounded wait, failed digests; the day, week, history and digest API; reviews and issues removed. Owner
check: the restart weeks' digests read against 0.14's Notion.

**N5 · The archive in the stack**, in two PRs.

- **N5a:** the compose file runs the archive (its image, with `pyarrow` and `zstandard` as base dependencies and
  `__version__` from the package metadata); the storage policy (the archive's `pdfs` setting and held jobs,
  litwatch asking for the PDFs it keeps, PDFs opened through `GET /api/papers/{id}/pdf`); the stack smoke test with a
  fake deep service and its CI job; `deep/` accepting legacy arXiv ids. Owner step: the owner's instance applies the
  PDF policy (`paper-archive pdfs wanted`).
- **N5b:** the relevance fixes in the archive (exact title and phrase first, title over abstract, checked read-only
  against the live index for speed) and litwatch keeping the archive's order with relevance by default when there
  is a query; paper pages for archive papers not yet fetched, Process showing the fetched state (#56), Deep read
  outside the collection; the archive's size for the search box; "Use the PDF" (#59, migration 0026 if it needs
  one); the archive's `backup.py` and `recovery.py` items (archive.md); the `00README` assembly only if Briefs show
  it matters. *Check:* "attention is all you need" returns 1706.03762 first. Owner steps (`archive/README.md`, Exact
  titles and Backups): back up the catalog (`sudo /srv/arxiv-work/env/bin/paper-archive-backup --project
  /home/dell-server-central/projects/arxiv-archive`); install the archive into `/srv/arxiv-work/env` and restart the
  five `paper-archive*` services, the indexer before the next step; run `paper-archive index-titles` once (about two
  minutes on the owner's 3.17M papers). After the first export in the new layout, remove the flat layout's files
  from `/srv/backups/arxiv-catalog` with the README's command.

Deferred from #54 (the owner, 2026-10-15: a
heuristic is fine to start with): the archive's text assembly (`archive/src/arxiv_archive/source_text.py`) picks a
source package's documents by heuristic, so it keeps only the shallowest of independent documents at different depths
(a root-level `cover.tex` beside `paper/main.tex` keeps the cover; the depth rule is there so standalone figure files
in subfolders stay out) and drops inputs nested deeper than eight levels. If Briefs show it matters, read arXiv's
`00README` directives, which name the top-level files, and take the PDF's text when a package stays ambiguous.
N5b found no sign of it: the ten Briefs of `evals/2026-10-15-briefs.md` all read their LaTeX (54,246 to 441,596
characters) and follow their papers. Look again at N6's Briefs; meanwhile "Use the PDF" reads a paper whose LaTeX
reads badly from its PDF.

**N5c · Shared flows**, after N5b and W2 merge (it changes their files) and before N6, so the restart runs on it.
#66 gave three steps every call site repeated one place each (`.env` at CLI entry, intake's settle in its batch, the
run record); four remain, found by the same search:

- **A paper change owns its locks and its settle.** About fifteen sites (classification, analysis, grading, the
  full-text and Deep workers, `feedback/word.py`, misses, `api/settings.py`, `api/reading.py`, digest picks) each open
  the transaction, take the locks, write and settle by hand. One transaction-owning flow in `feedback/word.py` takes
  the ordered locks, runs the action's checks and writes, and settles once before commit; Calibrate's labels write
  together inside it, so a moved Fit emits no intermediate unsave and save; digest picks lock in id order.
  `settle_topic`'s query moves into `store`. The owner approved its shape on 2026-10-16, and it is built so:
  `word.change(conn, *paper_ids)`, a context manager that settles each paper once at its exit, early returns
  included, and that a nested change joins.
- **One actionable-failure predicate** in `store/failures.py` (current revision, not dismissed, active topic), used by
  the lists, counts, detail and the guarded retry. It fixes `failures.waiting()`, which skips the active-topic test,
  and `grade_now`, which checks topic status outside the paper lock. `grading.record` checks the topic's status again
  under its locks (`word.change` holds the taxonomy lock shared) and drops a grading whose topic was paused during the
  call, as it already drops one for a rewritten or dismissed paper.
- **One bounded-concurrency helper** owning the tasks, completion order, cancel and drain, in place of the copies in
  classification, analysis, grading and the full-text worker; consumers hold its results with `async with`.
- **One current-Weight selector** in `store/analyses.py` beside `CURRENT_GIST`, with the revision guard and the
  ordering, used by `store/analyses.py`, `store/reading.py` and `feedback/calibrate.py`.

**N6 · Restart.** A fresh stack, the two most recent weeks read from the archive and run through the whole pipeline,
unlabelled; no data carried from 0.14 or the first v1 (decision 48). Check: the run's counts and costs, every Focus
central paper with its Brief or a stated reason, and the digests written. Also time the reading API's list and topic
queries on the restart's data with PostgreSQL's JIT on and off (`EXPLAIN (ANALYZE, BUFFERS)`, paired): at 100k
synthetic papers JIT added 100–800 ms per topic query (#52), since it compiles anew on every execution. Turn it off
for litwatch's connections if the full system shows the same.

Run with one week, not two (the owner, 2026-10-16: about $130 left on the Anthropic API, and the slice projected
$97–115 for two weeks): the papers submitted 2026-10-09 to 10-15, listed 10-12 to 10-16, under a $62 hard cap
(`evals/2026-10-16-restart.md`). The week before comes with the backfill. JIT stays on: at 2,951 papers no list
statement reaches `jit_above_cost`, and on and off time the same. The dearest one's estimate grows toward it with the
collection, so `evals/2026-10-16-restart/jit.py` runs again with the backfill or a month after the switch.

**W1–W4 · The web app** (Fable 5.1, worktree, PRs into `v1`). W1: the shell (sidebar, drawer, header, run status),
the look's tokens and faces, Papers, the Topics tab and topic page, the paper page with its ladder, rail and open
states. W2: Settings (Overview, Needs you, Calibrate, Misses, Topics and the editor with the re-run offer, Taxonomy,
Runs and cost with Jobs and the Deep card estimate, Integrations, Configuration). W3: the Digest's Day, Week, History (list and calendar) and the
digest slide-over. W4: phone gestures (edge swipe back, stepping, sheet drag), the PWA and share target, and the stack
smoke test's page list. A web PR starts on a surface only once its endpoints are merged.

### Deferred to later PRs

- **N8:** the n8n Notion workflow and its README still read the first v1's shapes (`take.written`, a topic's `take`,
  `kind: watch`). Rewrite them against v1's events (`brief.written`, `gist.written`, `digest.written`) with the
  switch, which owns the Notion export.
- **N6 (the owner, 2026-10-15):** the Area must-read volume (a median of 20 a night for Alignment and safety, 12 for
  Interpretability) and the full-text cost it brings (about $9 per 1,000 papers) are accepted for the restart.
  Reassess them after one to two weeks of running, together with the cost, and tune then. The Focus related ×
  incremental cell stays: two papers are not enough to move it. The Field offset is 2 (`evals/2026-10-15-tuning.md`).
- **W4 found**, for the smoke stack (`tests/stack`): a digest's own page and a Calibrate session's are walked
  only when the stack has one, and the fresh stack has neither. Give `smoke.sh` a written digest (a fake writer,
  as `fake_deep.py` fakes the deep service) and a drawn session, so `pages.py` walks them every run and `touch.py`
  steps a Day and a Calibrate card every run; the touch walk then also wants a Week step, the sheet's handle, a
  scroll inside the open drawer, and a drag's follow, resist and spring back, which only the compositor can show.
  A screen left by Back and changed shows its earlier copy under a drag back after a Forward: the copies are kept
  on PUSH only. A topic's digest strip keeps its 11 px squares on the phone (DESIGN.md asks 40 px of every
  control): a 40 px square per digest does not fit the card; the strip is a second way to the digests, which
  History lists at full size.
  Likewise a Calibrate session's strip (20 squares), whose 40 px way to a paper is the "3/20" picker. The way
  back shows the screen left under the finger for the last eight screens left; past them, the page colour.
  A paper's swipe steps through the rows its list had loaded when it was opened, as its "3/22" counts them;
  stepping past the loaded rows (a shelf's next page, a digest's papers beyond its preview), and an older entry
  from another list, which shows no place once a newer list has registered, are the list context's to grow
  (`web/src/lib/listContext.ts`). The icon (`web/public/icon.svg` and the PNGs) is still the
  placeholder serif "L" and needs a design pass later.
- **API gaps W1 found**, each for the core PR that owns it: a Brief on Paper (N3); a Deep card's queued state and
  "Write again", which POST answers 409 (N3's Deep cards); digests for the topic strips and the sidebar's counts (N4,
  W3); the "Among the
  collection" rows; rung ✓/✗ ratings; code and weights URLs on the facets; listing unclassified papers; splits and
  escalations on the status line; which source set the owner's word.
- **API gaps W2 found**, for the core PRs that own them: a jobs API (the editor's re-run offer, the Jobs page, a
  run's replay); a taxonomy write API (moving a topic to another class, the class editor, Import YAML); an outbox
  events endpoint (an integration's events page); Calibrate's class and Gist questions, and what an answer on a
  drawn topic that left Focus mid-session does (today a 422); "Resolved" on a miss, and the miss record keeping each
  stage's cells at the report (the trace is computed live meanwhile); typed response models for `/quality`, `/runs`
  and `/calibrate`; the Deep card's usage estimate on Runs and cost; ✓/✗ on a Gist and a Brief.
- **W2 or W3, the Deep card's web parts** (from N3's Deep cards): "Asked for" and the pulsing status line from
  `card_pass`; Write again on a paper that has a card (`card_requestable` is now true there; the phone layout and
  Actions still hide it); the "Among the collection" rows from `card.among` (rarity, relation, note, each linking to
  its paper) and links on prior work's arXiv ids; the card's label (revision, arXiv version, source, written); the
  `card_failed` open state; Quality's `deep` usage (attempts, list cost shown as "at least", unknown);
  `CardRequested.pass_id` in place of `event_id`. The pass reason `pick` (N4b, Area picks) beside `focus` and `request`.
- **API gaps W3 found**: the sidebar's per-topic count for today (DESIGN.md, "Shell": the count beside a followed
  topic where it has a digest today, highlighted for Focus) needs today's Marks on `/api/summary`, which carries the
  topics as badges only; the sidebar shows none until then. Today's date in the configured timezone (digests.md): the
  Digest's defaults (today's Day, the last complete Week, History's pinned week) use the browser's date, which is a
  day off for the hours the two timezones disagree, so `/api/status` (or the summary) should carry today's date.
  With it (the owner, 2026-10-15): the phone layout's page wrapper becomes `<main>`, as on the desktop, and the stack
  smoke test checks `<main>` again instead of `.screen`.

## Decisions

**D1 · The first v1's deployment is taken down now, and nothing is carried from it or from 0.14.** Its containers are
stopped, not removed, and its database was dumped to `~/backups/litwatch-v1-first-build-2026-10-14.dump` first. The
restart reads the two most recent weeks from the archive (decision 48 as amended). *Why:* the owner reads 0.14
meanwhile and is happy with it; keeping a deployment of the branch working would have meant dual paths in every
milestone. Not taken: freeze the deployment on its image; keep deploying the branch.

**D2 · The web app starts after N2b, in parallel with the core.** *Why:* the UI is built on real rows, and the owner
can steer it weeks earlier; the cost is API churn the web track follows (R1). Not taken: start after N6.

**D3 · The archive's code moves in first, as its own PR.** *Why:* N2a, N3 and N5 then change the archive and its
client in one PR each, and a disposable archive for verification exists from N3. Not taken: move it at N5. The code moves as one squashed commit (owner, 2026-10-14): the history and the instance's
records stay in the old repository, so this one stays small and publishable. Not taken: the whole repository with its
history (about 30 MB, mostly the instance's records); the code with its history.

**D4 · N0 takes what exists; the owner does not hand-label 200 papers.** The tree is taxonomy.md as agreed; the CoT
topics keep 0.14's definitions; the Area and Field topics take 0.14's subjects; the two new topics were drafted and
agreed on 2026-10-14. *Why:* 200 hand labels would take the owner a week. Not taken: build on while waiting for
labels; wait for each gate.

**D5 · The main agent merges into `v1`** once review converges and checks pass, as in M3–M6. A PR whose milestone
check is an owner check (N1's default, N4's digests, the web reads) is listed as pending and does not merge until the
owner has done it. The owner gets a short summary per merged PR. Not taken: the owner approves each PR.

**D6 · Class labels come from what exists plus a two-lab silver sample** (decision 55): Focus recall on the owner's
labels and 0.14's CoT positives; 0.14's tiers as silver memberships; Opus 5.5 and GPT-6.1-Sol on a stratified sample
for primary class and the rest; the owner settles at most 20 disagreements. Reported per cohort, never pooled.
*Why:* recall, the check that matters, is measured on the owner's word as before, and the shallow classes need only
silver. Not taken: silver without the owner's 20 (it leaves out the hard papers); 200 hand labels.

**D7 · Fit is closeness to a topic, Weight is importance in the paper's own area** (decisions 53, 54). One shared
definition of each level, written into every prompt that judges it. A topic's subject is its Fit definition; a Focus
topic's rubric spells out what central, related and adjacent mean for it. 0.14's must-read bar becomes the Weight definition.
Weight is one per paper, judged against the paper's primary class; a per-topic Weight is added only if "overrated"
reactions show the need. Not taken: a rubric per topic at every depth (the bar would count twice).

**D8 · The old pipeline goes when its input goes, not when its replacement lands** (owner, 2026-10-14). N2a
removes triage, the keyword audit, candidates, takes, Watch grading, field reviews, issues, the triage-based replay,
metrics, miss traces and reject samples, and the carrying of data from 0.14 and the first v1 (decision 48). Grading
returns in N2b, digests in N4, recall per stage with N2c's replay. *Why:* from N2a on these get no input; keeping them
alive meant adapters for code deleted one to three PRs later. Not taken: keep each old step running on the new data
until its replacement lands.

**D9 · The old web app is retired at N2a** (owner, 2026-10-14). `web/` becomes a minimal shell that still builds,
typechecks and tests in CI; W1 builds the new app on it, and the old components stay in git history to borrow from.
Between N2a and W1, data is checked through the CLI and the API. *Why:* W1–W4 rewrite every page, and nothing reads
the branch's UI before then. Not taken: prune page by page, patching pages W1 deletes.

## Risks

- **R1 · The web track chases a moving API.** A web PR starts on a surface only once its endpoints are merged; the
  regenerated `schema.d.ts` makes a break a type error in CI; a core PR that changes an endpoint the web uses fixes
  its call sites in the same PR.
- **R2 · No decision model reaches the first v1's Focus recall (N1).** Membership recall (band and keyword check) is
  measured apart from classification recall. If it still falls short, the work stops and the misses and options
  (lower thresholds, a chat model with a strict schema, richer class descriptions) go to the owner before N2 builds on
  them.
- **R3 · Evidence runs cost more than $5 per change.** The N0 silver sample and the N1 bake-off stay under it. N2c's
  two-week run (about 9,000 papers, Area grading on Opus) and N6 are likely $20–40 each: the estimate is shown and the
  owner asked before each.
- **R4 · Touching live services by mistake.** The live archive is only read (metadata, search); registrations and
  urgent fetches go to a disposable archive; deep runs against a dev instance; Zotero only the test account.
  Production 0.14 is never written.
- **R5 · The restart misses something.** N6 runs first on the sandbox in full; its check lists every Focus central
  paper of the window with its Brief or the reason it has none, and the counts per step against the estimates.
- **R6 · The UI drifts from the sketches.** Each web PR puts its pages beside the matching sketch at 390 and 1440 px,
  dark and light; differences are fixed or named in the PR; the owner reads W1 and W3.
- **R7 · A long build loses its thread.** This plan, the milestone checks and the `evals/` reports are the log; each
  merged PR updates "Where things stand" in milestones.md; deferred review findings go into the milestone that owns
  them.
- **R8 · Circuits and causal analysis starts with no labels at all.** 0.14 had no selection for it (`mech-interp` was
  Interpretability's earlier name). Its classification and membership recall rest on the silver sample and its keywords until
  the owner's reactions and misses on it become labels; Quality shows it as measured on silver. Its grading and
  end-to-end recall need Fit labels and are not reported until those exist.
- **R9 · Zotero duplicates at the switch.** v1 starts without 0.14's record of which papers have Zotero items, so a
  paper saved in both could get a second item. Settled in N8's own plan.

## Verification

| | Claim | Evidence |
| --- | --- | --- |
| V1 | The archive builds and tests inside this repository; nothing deployed changed (A) | Its tests in CI; the import commit names its source commit; the live archive's status unchanged |
| V2 | The tree loads, every class is described, every topic has its subject and every Focus topic its rubric, and the evaluation labels exist per source (N0) | pytest on the taxonomy rules and the YAML round trip; label counts per class and source; the owner's adjudication committed |
| V3 | A default classifier is chosen with threshold and band, and Focus membership recall on the CoT positives is at least the first v1's candidate recall (N1) | The report in `evals/`; one real `decide` call per provider; the owner's pick |
| V4 | Classification, analysis, grading per depth, the grade table, rarity, effective grades and the library rule do what pipeline.md says (N2a–b) | pytest per rule on real Postgres (section 4's grading-topic examples, both tables, the floors, the lab-diversity refusal, Area escalation); a sandbox night with its run record |
| V5 | End-to-end Focus recall is at least the first v1's, or each loss looked at; cost per weekday measured (N2c) | `eval replay` and `eval report` per cohort; the two-week sandbox run; the cost table in pipeline.md |
| V6 | Every Focus central or related paper has a Brief and every Area must-read candidate a re-checked Weight within the wait, or shows failed or awaiting text; a paper with a Brief shows the Gist written with it; every finished Deep attempt, carded or failed, has its usage or says it is unknown (N3) | A test night on the sandbox against a disposable archive; pytest for the lease, revisions, what a pass read, the current Gist and Deep usage; the overstatement line on ten hand-read papers, whose Gists repeat no claim the Brief corrects (owner check, added to the preference set) |
| V7 | Digests per depth for the restart weeks; every Focus central paper appears; Area dailies have a few picks at most (N4) | pytest for due, quiet days, records, the wait and failed digests; the owner's read against 0.14's Notion; the harness on digest voice |
| V8 | "attention is all you need" returns 1706.03762 first (N5b); a fresh stack with an empty archive ingests a night and serves full text (N5a) | A search test in `archive/`; the stack smoke test (seeded papers in CI) and one night read from arXiv on a fresh stack, in N5a's report |
| V9 | The restart produces a readable v1 from nothing (N6) | Counts and cost per step for the window; every Focus central paper with its Brief or reason; digests written |
| V10 | Every page does what DESIGN.md says on both widths (W1–W4) | vitest for logic; each flow walked on the sandbox in T3's browser at 390 and 1440 px, dark and light, empty and quiet states, the way back from every action; screenshots beside the sketch; motion recorded; the owner's read |

## Scope

**In:** N0–N7 as the spec and DESIGN.md say, with the changes of 2026-10-14 (decision 48 as amended, decisions
53–56).

**Out:** N8 (side by side and the switch, the Zotero renames and R9): it touches production and the owner's library,
so it gets its own plan once N6 and W4 run on the sandbox. N9 (release). Backfill of the papers not processed,
through the same archive intake over older windows, likely on a local model; semantic search, citations, other
sources: after v1. Retiring the archive's own web interface: after the switch.
