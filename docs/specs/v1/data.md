# Modules, data and configuration

One Python service, one Postgres database, one web app served by that service, beside the archive and the optional
`deep` service. The dependency rule is the first v1's: `core` ← `store` ← pipeline packages ← `jobs` ← `api` and
`cli`; `llm` and `events` know nothing about the pipeline; integrations use only `events`, `store` and `core`; nothing
imports `api`.

## Modules

| Package | First v1 | Now |
| --- | --- | --- |
| `core` | domain types, pure rules | adds `Class`, `Membership` (class, probability, source), `Analysis`, `Fit`, `Weight`, `Facets`, `Brief`, `FullTextPass`, `Digest`, `Pick`; pure rules `grade(fit, weight)`, `rarity(...)`, `grading_topics(paper_classes, tree, followed)` |
| `intake` | arXiv → papers | unchanged; registers papers with the archive through `archive` |
| `archive` | (an integration) | **new package**: the archive client (register, request at a priority, read text, read PDF, search) |
| `llm` | gateway, chat | adds the decision request shape ([models.md](models.md)) |
| `screening` | triage, keyword audit | **becomes `classification`**: the decision request built from the taxonomy, thresholds, uncertain band, keyword check; pure `classify(papers, tree)` and a separate `record` |
| `analysis` | — | **new**: Gist, Weight, facets |
| `watch` | Watch grading | **becomes `grading`**: Fit per followed topic at its assurance level, reconciliation, escalation |
| `fulltext` | — | **new**: the queue, Briefs, Weight re-check, handing Deep card requests to the `deep` integration |
| `review` | reviews, takes, issues | **becomes `digests`**: daily and weekly digests per depth, their Gists, picks |
| `publications` | DBLP, BibTeX | unchanged |
| `feedback` | labels, reactions, misses, evaluation | reasons per stage, class labels, owner-first misses, recall per stage |
| `events`, `integrations`, `jobs`, `api`, `cli` | | as before; `archive` leaves `integrations` |

As in the first v1, every pipeline step is a pure function that writes nothing plus a separate `record`, so
evaluation calls the same functions and writes only to evaluation tables.

## Data

**New tables:**

- `classes` (id, parent, name, description, threshold, position, version) and `class_versions` (class, revision, hash,
  content, saved at): the taxonomy, edited in Settings.
- `topics` is reshaped: (class, depth focus|area|field, subject, rubric, interests, keywords, monogram, colour,
  position, status, version). A topic points at one class; the class's description is what the classifier reads, the
  topic's subject and rubric are what the graders read. Unfollowing a class deletes its topic row; its versions stay in
  `topic_versions`, and following it again continues from the last one. A class taken out of the tree is retired, its
  row kept; putting the same slug back brings it back, with its memberships.
- `classifications` (paper, paper revision, taxonomy revision, primary class, the decision response with every class's
  probability, model, at; the taxonomy revision is the hash of every class's version in tree order, which is what the
  request is built from) and `memberships` (paper, class, paper revision, class version, source classifier |
  uncertain | keyword | owner | miss, member yes | no, probability): direct memberships only, a "no" being the
  owner's class label removing the paper from a class; inherited ones come from the tree, and
  the classifier's raw outcome stays in `classifications` whatever the owner says. Memberships are the
  current projection, replaced by each classification; their history is `classifications`, whose probabilities replay
  any threshold.
- `analyses` (paper, paper revision, gist, weight, weight reason, facets, model, at).
- `weights` (paper, paper revision, weight, source abstract | full_text, pass, reason, at): the current Weight is the
  one from the full-text pass queued last for the current revision, the same pass as the current Brief and Gist, else
  from the abstract.
- `verdicts` (paper, topic, topic version, paper revision, fit, reason, opinions per grader, split, escalated, at),
  one row per grading as before; the topic version is the topic's version hash, and opinions are keyed by role
  (`judge`, `check`, `area_check` with its probability, `field_grade`). The grade is derived, not stored here.
- `effective_grades` (paper, topic, fit, grade, rarity on the topic's page), a row for every followed topic with a
  verdict or the owner's word, and `rarities` (paper, mixed-list level, best topic, null when no topic graded the
  paper and its Weight's floor alone sets the level, and the current Weight the level was set with): kept current so every surface reads one answer and lists sort
  by it.
- `step_failures` (paper, step classification | analysis | grading, topic for a grading, paper revision, failed
  runs, last error): a step's failed runs on the paper's current text; three unusable answers in a row leave it for
  the owner (pipeline.md, section 9).
- `grading_passes` (paper, paper revision, at): the revision's first grading pass is done; after it only failed
  gradings and reported misses are graded (pipeline.md, section 4).
- `gists` (paper, paper revision, arXiv version or unknown, pass, text, source abstract | full_text, model, at): the
  current Gist is the one from the full-text pass queued last for the current revision, else from the abstract. `analyses.gist` is the abstract one.
- `briefs` (paper, paper revision, arXiv version or unknown, pass, text, overstatement line, text source latex | pdf,
  text hash, model, at): the current Brief is the one from the pass queued last for the current revision, the same pass
  as the current Gist.
- `fulltext_passes` (paper, paper revision, kind brief | weight | deep, reason, state awaiting_text | ready | running |
  done | failed, priority, queued at, tried at, text arrived at, arXiv version read, text source latex | pdf, text
  hash, paper's update read for, lease until, failed runs, retry text, use the PDF (the owner's, for LaTeX that reads
  badly), last error): one Brief or Weight re-check open per paper revision, and one
  Deep pass open per paper (reason request | focus | pick; its revision is the one it read); a worker takes the least recently
  tried first. `weights` and `gists` name the pass a full-text row came from.
- What a digest still has to cover is not stored: a paper is due for a topic while it is eligible there, was announced
  within three weeks of the digest's day, and is in no published digest of the topic that clears it (a daily one for
  Focus and Area, a weekly one for Field), found from the database when a digest is written
  ([digests.md](digests.md)). `papers.announced` is the date of the arXiv listing that first showed the paper.
- `digests` (topic, period day | week, day (a week's Monday), state written | nothing_new | failed, gist, text, counts,
  best rarity, error, run, written at; published means written or nothing_new) and `digest_papers` (digest, paper,
  role pick | listed | mentioned, position, why, and frozen at writing: paper revision, title, gist, fit, grade, rarity (a
  mixed list's), and the topic whose grade it showed).
- `text_ratings` (target gist | brief | digest, id, accurate | off, at), replacing `take_ratings`; a Gist rating
  names the `gists` row it rated.
- `eval_labels` (evaluation run, paper, topic, cohort, positive): the labels a replay is measured against, frozen when
  it starts, so its numbers read the same however the label files or the misses change ([feedback.md](feedback.md),
  evaluation).

**Removed:** `triage`, `candidates`, `takes`, `take_ratings`, `issues`, `issue_entries`, `reviews`,
`review_picks`, `set_asides` (a digest reads the set-aside mark live from the effective grade), `deep_waiting` (a Deep pass
awaits its text); the first v1's
`memberships` (paper in a field) is replaced by the new one. `regression` and the M2 labelling tables stay as
evaluation history.

**Kept as they are:** `papers`, `intake_checkpoints`, `intake_snapshots`, `runs`, `inference_calls` (plus the batch a call went in, and the reply of a failed call, kept in debug mode), `labels` (plus
class labels; the value is a Fit or unsure, and the grade column is gone: the grade follows from the Fit), `reactions` (reason values changed: wrong_topic, overrated, not_interesting, already_knew, underrated, great_find), `appraisals`, `calibration_sessions`,
`topic_visits`, `misses` (plus the per-stage record), `calibration_items` (plus the pipeline's frozen Fit and grade at
the draw), `publications`, `library`, `outbox`, `integration_cursors`,
`zotero_items`, `webdav_files`, `deep_cards` (plus what it was written from: paper revision, arXiv version, text
source, pass; the job columns are gone), `deep_attempts` (plus pass, job, ended at, error, and the job's usage: tokens,
turns, list cost, null when unknown), `eval_runs` (plus every run record a
resumed replay ran under, whose costs are its cost), `eval_results` (columns for classification instead of triage:
whether the classifier alone and the uncertain band made the paper a member, the highest probability in the topic's
subtree, and the graded Fit; the keyword hit, both opinions, split and the two errors as before), `eval_samples`,
`schema_migrations`.

The restart ([milestones.md](milestones.md), N6) starts from an empty database (decision 48): the migrations run on
a fresh schema, and the most recent week is read from the archive (two weeks until the owner's call of 2026-10-16;
the week before comes with the backfill). Nothing is carried from 0.14 or the first
v1; the first v1's database was dumped before its deployment stopped (2026-10-14).

The owner's earlier labels are kept as the evaluation set, as files in `evals/labels/` (decision 55), mapped onto the
new terms. Each mapped row keeps its first-v1 value in a `legacy` column, so nothing is lost if a mapping turns out
wrong:

| Before | Becomes |
| --- | --- |
| first-v1 label relevant (with or without a grade) on a Watch topic | Fit central on the Focus topic for the same class; the old grade only in `legacy` |
| label adjacent / not relevant / unsure | Fit adjacent / none / unsure |
| provisional label | provisional, in its own cohort ([feedback.md](feedback.md), evaluation) |
| a label origin (gold, reader, sample, miss) | kept as it is |
| a 0.14 selection tier | a silver membership of the matching class: tier 1–2 member, tier 4 not, tier 3 left out |

The first v1 had no reactions, misses or take ratings to map.

## Configuration

`litwatch.toml` stays the source of truth and read-only in the web app (first v1, decision 11). Changed sections:

```toml
debug = false              # true, or LITWATCH_DEBUG=1: keep failed calls' replies (models.md)

[models.classify]          # decision model, chosen in N1
provider = "openrouter"
model = "typesafe/jev-1.13"
decisions = true           # OpenRouter's Decisions endpoint

[models.analyse]
provider = "anthropic"
model = "claude-haiku-5-5"

[models.judge]
provider = "anthropic"
model = "claude-opus-5-5"

[models.check]
provider = "openrouter"
model = "meta/muse-spark-1.3-contributor"

[models.area_check]
provider = "openrouter"
model = "typesafe/jev-1.13"
decisions = true

[models.field_grade]
provider = "anthropic"
model = "claude-haiku-5-5"

[models.brief]
provider = "anthropic"
model = "claude-sonnet-5-5"
effort = "high"
fallback = "brief_fallback" # asked once when brief refuses (models.md, roles)

[models.brief_fallback]    # another lab, chosen by the model-spend plan's replay
provider = "openrouter"
model = "google/gemini-4-argon"

[models.writer]
provider = "anthropic"
model = "claude-opus-5-5"

[classification]           # chosen in N1
default_threshold = 0.8    # a class without its own threshold
focus_threshold = 0.5      # a class at or under a Focus topic
uncertain_floor = 0.1

[archive]                  # required
url = "http://archive:8787"
pdfs = "followed"          # "followed" (the storage policy) or "all"

[grading]
area_check_min_confidence = 0.7

[digests]
wait_minutes = 120

[budget]
anthropic_monthly = 100
openrouter_monthly = 25

[schedule]
nightly = "0 2 * * *"
deadline = "07:30"         # the nightly's batches end here (models.md, batches)
timezone = "America/Denver" # the owner's; none is the server's own zone
```

The models are proposals until N1 and N2 measure them. The taxonomy and topics live in the database, edited in
Settings, with YAML import and export; [taxonomy.md](taxonomy.md) is the seed.
