# Milestones

## Where things stand

M0–M6 of the first v1 are built on `v1` (PRs #15–#32): package layout, migrations, the model gateway, intake,
topics, triage, grading with two labs, feedback and the evaluation, the API and web app, reviews and issues, the
scheduler, DBLP, the outbox and integrations, the 0.14 migration. The `litwatch-v1` stack ran it beside 0.14 with the
scheduler on until 2026-10-14; it is now stopped. Most of that code stays: the redesign replaces triage, takes, reviews and
issues, and adds classification, analysis, the full-text queue and digests.

The milestones below replace the first spec's M7 for everything up to the switch. Each ends with something that runs
and a stated check. Each milestone is one or more PRs, into `v1` until the switch and into `main` since, shipped as
[`docs/agents/workflow.md`](../../agents/workflow.md) says. The order of work, the PRs and their evidence are in
the build plan, [`docs/plans/v1-build.md`](../../plans/v1-build.md) (approved 2026-10-14): the archive's code moves
in first (decision 49), and the web app (N7) is built from N2 onward, beside the core.

Built for the redesign: the archive's code in `archive/` (#41); N0, the seed tree with its topics and the evaluation
labels (#42); N1, the decision request and the classifier comparison, with Jev chosen (#43,
`evals/2026-10-14-classifiers.md`); N2a's teardown, the first v1's pipeline and web app taken out (D8, D9 in the build
plan; the web app a placeholder until N7); N2a's taxonomy in the database, the class tree and its topics versioned in
`classes` and `topics`, imported and exported as YAML (`litwatch taxonomy import`, `export`); N2a's intake from the
archive; N2a's classification and analysis as nightly steps (records, events, awaiting classification with the owner's
three actions); N2b's grading (Fit per depth, the grade table, effective Fit and grade, rarity, the library rule and
budgets; the nightly run is intake, classification, analysis and grading); N2b's reading API (Papers rarest first with
its filters, search and counts per rarity; the topics by depth and a topic's page under the parent rule; a paper's page
with its rail, open states and history; reaction reasons as feedback.md names them); N2c's replay (`litwatch eval replay` and `eval report`: the four recall
numbers per cohort and Focus topic over labels frozen with the run, resumable, and recall from the latest replay on
Quality; the first run in `evals/2026-10-15-replay.md`); N3's text reads (the archive's LaTeX text with its arXiv
version, urgent requests, a disposable archive for verification) and its full-text queue (Briefs with the
overstatement line, the Gist and Weight from the full text, the lease, revisions, Quality and the status line,
`litwatch fulltext`); N4's digests (due found from the database, daily and weekly digests per depth with their Gists
and picks, quiet days, records with the live set-aside mark, the bounded wait in the nightly run, failed digests on
Quality and the status line, the Day, Week, History and digest API with the topics' digest strips, `litwatch digests
write` for a window and `litwatch eval digests` for the voice check); N3's Deep card passes (each attempt's usage or that it is unknown, the lease, cards labelled
with the text they read, Write again, the "among the collection" section in `deep/`) and its Briefs harness
(`litwatch eval briefs`, the ten papers in `evals/2026-10-15-briefs.md`); N5a's archive in the stack (the compose file runs the archive, the PDF storage policy with held jobs and PDFs fetched when opened, the stack smoke test in CI); N5b's search and papers outside the collection (exact title and phrase first in the
archive, its order kept; pages for archive papers not fetched, the fetched state, Deep read outside the collection,
the archive's size in the search box; Use the PDF; a PDF read without the AGPL extra; the archive's whole-export
backup and one-transaction recovery reconcile). N6, the restart (#71,
`evals/2026-10-16-restart.md`), over one week rather than two by the owner's call of 2026-10-16 (the week before comes
with the backfill). Next: the owner's read of N3's ten Briefs; the owner's instance applies the PDF
policy. The model-spend plan ([`docs/plans/model-spend.md`](../../plans/model-spend.md), decisions 60–62) brought
the nightly under the owner's caps before the switch (#74, #75). N8, the switch
([`docs/plans/v1-deploy.md`](../../plans/v1-deploy.md)): the telemetry (#77), and v1 in place of 0.14 on the night of
2026-10-16, accepted by the owner on 10-17 ([`evals/2026-10-17-switch.md`](../../../evals/2026-10-17-switch.md)), when
`v1` merged into `main`. Next: N8's P4, Zotero and WebDAV after a few stable nights and the removal of 0.14.

On 2026-10-14 the first v1's deployment was stopped (its database dumped first); the owner reads 0.14 until the
restart (decision 56).

**N0 · Taxonomy, topics and evaluation labels.** [taxonomy.md](taxonomy.md) becomes the seed tree with its followed
topics. The evaluation set is built from what exists, as files in `evals/labels/` (decision 55): the owner's first-v1
labels, the provisional positives and the regression set; 0.14's selection tiers as silver memberships; and 200 papers
drawn with a recorded seed (stratified over 0.14's selections, CoT positives and labelled papers included), labelled against the tree by two models of different labs, with the owner settling the papers on
which they name different primary classes (at most 20). *Check:* the tree loads, every class has a description, every
topic has its subject and every Focus topic its rubric, and the label counts per class and cohort are reported.

**N1 · Classifier comparison.** The decision request shape in `llm`; the classification request built from the tree;
Luna Decisions, Jev and Haiku 5.5 run on the N0 evaluation set. Reported per candidate and per cohort, never pooled, each
metric on the cohorts that carry its label: recall per Focus class at the threshold and with the uncertain band,
primary-class accuracy, per-class precision on the top levels, cost per 1,000 papers, latency, failure rate. *Check:* a written report in `evals/` and a chosen default,
with the threshold and band set from it; Focus membership recall (band and keyword check included) on the CoT
positives at least as high as the first v1's candidate recall.

**N2 · The new core.** Classification and analysis as nightly steps; grading per followed topic at its assurance level
with Area escalation; the grade table and rarity; the library rule; budgets; replay with the four recall numbers.
*Check:* run over the two most recent weeks, read from the archive, plus the evaluation papers; end-to-end Focus recall on the owner's labels and the
0.14 positives at least as high as the first v1's, or each loss looked at; grade-table and rarity offsets tuned on the
labels; measured cost per weekday written next to the estimates in [pipeline.md](pipeline.md).

**N3 · The full-text queue.** Urgent archive requests, text reading (LaTeX first), Briefs with the overstatement line
and the Gist from the full text, Weight re-checks, Deep card requests through the queue, the "among the collection" section and linked prior work in
`deep`, and its usage recorded per attempt. *Check:* on a test night, every Focus paper at central or related has a Brief and every Area must-read
candidate a re-checked Weight within the digest wait, or its pass is on Quality as failed or awaiting text and the
digest used the abstract; every paper with a Brief shows the Gist written with it, and every finished Deep
attempt, carded or failed, has its usage or says it is unknown (a job lost before it reported); on ten hand-read papers, the overstatement line appears where
the owner agrees the abstract overstates and nowhere else, and no Gist repeats a claim its Brief corrects.

**N4 · Digests.** Daily and weekly digests per depth, their Gists, picks, quiet days, records, the bounded wait, the
History, day and digest API. *Check:* digests for the restart weeks, read by the owner against the same weeks in 0.14's
Notion; every Focus central paper of the week appears; the Area daily has at most a few picks on an ordinary day.

**N5 · The archive in the stack.** The archive's code is already in `archive/` (moved first, per the build plan); the
storage policy; relevance fixes in
the archive (exact title and phrase first, title over abstract) and in litwatch (keep the archive's order, relevance by
default with a query); paper pages and Process / Deep read for papers outside the collection; the compose file runs the
archive. *Check:* "attention is all you need" returns 1706.03762 first; a fresh stack with an empty archive ingests a
night and serves full text for it; the owner's instance applies the PDF policy.

**N6 · Restart.** A fresh stack with an empty database (decision 48): the two most recent weeks (one, by the owner's
call of 2026-10-16) are read from the
archive and run through the whole pipeline, unlabelled. *Check:* the counts and cost per step against the estimates;
every Focus paper at central with its Brief, or the reason it has none; the digests of the window written.

**N7 · The web app.** [`DESIGN.md`](DESIGN.md), built in four parts (shell and reading pages, Settings, the Digest,
phone gestures and the PWA) as the build plan says. *Check:* every page walked on the sandbox at phone and desktop
widths, dark and light, beside its sketch; the owner's read.

**N8 · The switch** (the first spec's M7, adapted; the plan is [`docs/plans/v1-deploy.md`](../../plans/v1-deploy.md)).
v1 takes 0.14's place on the owner's server rather than running beside it (decision 63): 0.14 stops after its last
daily run and v1 starts on an empty database, the last week read from the archive and processed by the scheduled
nightly. The switch holds when v1's Focus output for that week contains everything 0.14 put at tier 1–2 on the two CoT
selections for the same papers, or each difference has been looked at (decision 64); otherwise 0.14 comes back. Before
it, litwatch follows the homelab's telemetry convention: JSON logs, metrics with the shared job metrics, a status
endpoint (decision 65). Zotero and WebDAV stay off for a few stable nights and then catch up from the first event; the
collections are renamed then (decision 66). Notion stops at the switch (decision 67). The owner removes 0.14 by hand.
Carried notes: do not share v1's Postgres with services holding transactions open; check a resumed WebDAV upload's
md5 and mtime against the real library once.

**N9 · Release** (the first spec's M8, unchanged): compose file, install and configuration docs, repository clean-up,
MIT licence, `v1.0.0`; `evals/` and homelab specifics leave the repository; the AGPL `pdf` extra stays optional.

After v1: backfill of the archive's older papers (the owner's plan: a local decision-style model over several nights,
for example DiffusionGemma with constrained decoding, then leftover monthly credit on the stronger graders for what it
surfaces); semantic search and a citation graph in the archive; other sources.
