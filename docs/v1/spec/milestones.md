# Milestones

## Where things stand

M0–M6 of the first v1 are built on `v1` (PRs #15–#32): package layout, migrations, the model gateway, intake,
topics, triage, grading with two labs, feedback and the evaluation, the API and web app, reviews and issues, the
scheduler, DBLP, the outbox and integrations, the 0.14 migration. The `litwatch-v1` stack runs it beside 0.14 with the
scheduler on, Zotero, WebDAV and Notion off. Most of that code stays: the redesign replaces triage, takes, reviews and
issues, and adds classification, analysis, the full-text queue and digests.

The milestones below replace the first spec's M7 for everything up to the switch. Each ends with something that runs
and a stated check. The web app's rework is planned by the second pass (UI sketches) and slots in as N7. Each
milestone is one or more PRs into `v1`, each with a local Astra review and the bot rounds.

**N0 · Taxonomy and labels.** The owner edits [taxonomy.md](taxonomy.md) into the seed tree. litwatch draws 200 papers
from the side-by-side collection with a recorded seed (stratified so every top-level class has some, plus the 0.14
CoT positives and the labelled papers), and the owner assigns each its classes on a quick page (title, abstract, a
pick list; most papers take a few seconds). *Check:* the tree loads, every class has a description, 200 papers carry
class labels.

**N1 · Classifier comparison.** The decision request shape in `llm`; the classification request built from the tree;
Luna Decisions, Jev and Haiku 5.5 run on the N0 sample and the CoT evaluation set. Reported per candidate: recall per
Focus class at the threshold and with the uncertain band, primary-class accuracy, per-class precision on the top
levels, cost per 1,000 papers, latency, failure rate. *Check:* a written report in `evals/` and a chosen default,
with the threshold and band set from it; Focus membership recall (band and keyword check included) on the CoT
positives at least as high as the first v1's candidate recall.

**N2 · The new core.** Classification and analysis as nightly steps; grading per followed topic at its assurance level
with Area escalation; the grade table and rarity; the library rule; budgets; replay with the four recall numbers.
*Check:* run over the last week plus the evaluation papers; end-to-end Focus recall on the owner's labels and the
0.14 positives at least as high as the first v1's, or each loss looked at; grade-table and rarity offsets tuned on the
labels; measured cost per weekday written next to the estimates in [pipeline.md](pipeline.md).

**N3 · The full-text queue.** Urgent archive requests, text reading (LaTeX first), Briefs with the overstatement line,
Weight re-checks, Deep card requests through the queue, the "among the collection" section and linked prior work in
`deep`. *Check:* on a test night, every Focus paper at central or related has a Brief and every Area must-read
candidate a re-checked Weight within the digest wait, or its pass is on Quality as failed or awaiting text and the
digest used the abstract; on ten hand-read papers, the overstatement line appears where
the owner agrees the abstract overstates and nowhere else.

**N4 · Digests.** Daily and weekly digests per depth, their Gists, picks, quiet days, records, the bounded wait, the
History, day and digest API. *Check:* digests for the restart week, read by the owner against the same week in 0.14's
Notion; every Focus central paper of the week appears; the Area daily has at most a few picks on an ordinary day.

**N5 · The archive in the stack.** The archive's code moved per decision 49; the storage policy; relevance fixes in
the archive (exact title and phrase first, title over abstract) and in litwatch (keep the archive's order, relevance by
default with a query); paper pages and Process / Deep read for papers outside the collection; the compose file runs the
archive. *Check:* "attention is all you need" returns 1706.03762 first; a fresh stack with an empty archive ingests a
night and serves full text for it; the owner's instance applies the PDF policy.

**N6 · Restart.** The migration of [data.md](data.md) on a copy of the side-by-side database: the last week through the
full pipeline, the evaluation papers, everything the owner's feedback touches; the rest dropped. *Check:* the counts
of kept and dropped papers, and no label, reaction, saved paper, Zotero item or Deep card lost.

**N7 · The web app.** From the second pass. Its own check comes with its plan.

**N8 · Side by side and switch** (the first spec's M7, adapted). A few days beside 0.14 with the scheduler on and the
owner reading v1 every morning; Zotero stays off until the switch and its collections are renamed then. The switch
happens when v1's Focus output for those days contains everything 0.14 put at tier 1–2 on the two CoT selections, or
each difference has been looked at. The owner removes 0.14 by hand. Carried notes: do not share v1's Postgres with
services holding transactions open; check a resumed WebDAV upload's md5 and mtime against the real library once.

**N9 · Release** (the first spec's M8, unchanged): compose file, install and configuration docs, repository clean-up,
MIT licence, `v1.0.0`; `evals/` and homelab specifics leave the repository; the AGPL `pdf` extra stays optional.

After v1: backfill of the archive's older papers (the owner's plan: a local decision-style model over several nights,
for example DiffusionGemma with constrained decoding, then leftover monthly credit on the stronger graders for what it
surfaces); semantic search and a citation graph in the archive; other sources.
