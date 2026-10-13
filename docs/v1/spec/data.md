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
- `topics` is reshaped: (class, depth focus|area|field, subject, rubric, interests, keywords, icon, colour, position,
  status, version). A topic points at one class; the class's description is what the classifier reads, the topic's
  subject and rubric are what the graders read.
- `classifications` (paper, paper revision, taxonomy revision, primary class, the decision response with every class's
  probability, model, at) and `memberships` (paper, class, paper revision, class version, source classifier |
  uncertain | keyword | owner | miss, probability).
- `analyses` (paper, paper revision, gist, weight, weight reason, facets, model, at).
- `weights` (paper, paper revision, weight, source abstract | full_text, reason, at): the current Weight is the newest
  from full text, else from the abstract.
- `verdicts` (paper, topic, topic version, paper revision, fit, reason, opinions per grader, split, escalated, at),
  one row per grading as before; the grade is derived, not stored here.
- `effective_grades` (paper, topic, grade) and `rarities` (paper, mixed-list level, best topic): kept current so every
  surface reads one answer and lists sort by it.
- `briefs` (paper, paper revision, text, overstatement line, text source latex | pdf, model, at).
- `fulltext_passes` (paper, kind brief | weight | deep, reason, state awaiting_text | ready | running | done |
  failed, priority, queued at, text arrived at, failed runs, last error).
- `digests` (topic, period day | week, day, state written | failed | nothing_new, gist, text, counts, best rarity,
  written at) and `digest_papers` (digest, paper, role pick | listed | mentioned, order, why).
- `text_ratings` (target gist | brief | digest, id, accurate | off, at), replacing `take_ratings`.

**Removed:** `triage`, `candidates`, `due`, `takes`, `take_ratings`, `issues`, `issue_entries`, `reviews`,
`review_picks`; the first v1's `memberships` (paper in a field) is replaced by the new one. `set_asides` is kept for
digests (a listed paper the owner later set below relevant). `regression` and the M2 labelling tables stay as
evaluation history.

**Kept as they are:** `papers`, `intake_checkpoints`, `intake_snapshots`, `runs`, `inference_calls`, `labels` (plus
class labels), `reactions` (reason values changed), `appraisals`, `calibration_sessions`, `calibration_items`,
`topic_visits`, `misses` (plus the per-stage record), `publications`, `library`, `outbox`, `integration_cursors`,
`zotero_items`, `webdav_files`, `deep_cards`, `deep_attempts`, `deep_waiting`, `eval_runs`, `eval_results` (columns
for classification instead of triage), `eval_samples`, `schema_migrations`.

The restart ([milestones.md](milestones.md), N6) is a new migration on a copy of the side-by-side database: papers
outside the restart set and their dependent rows are dropped; labels, reactions, appraisals, misses, library state,
publications, Zotero and WebDAV state and Deep cards are kept for every paper that has them, and those papers are part
of the restart set.

## Configuration

`litwatch.toml` stays the source of truth and read-only in the web app (first v1, decision 11). Changed sections:

```toml
[models.classify]          # decision model
provider = "openrouter"
model = "openai/gpt-6-luna-decisions"

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
model = "openai/gpt-6-luna-decisions"

[models.field_grade]
provider = "anthropic"
model = "claude-haiku-5-5"

[models.brief]
provider = "anthropic"
model = "claude-opus-5-5"

[models.writer]
provider = "anthropic"
model = "claude-opus-5-5"

[classification]
default_threshold = 0.5
uncertain_floor = 0.2

[archive]                  # required
url = "http://archive:8787"
pdfs = "followed"          # "followed" (the storage policy) or "all"

[digests]
wait_minutes = 120

[budget]
anthropic_monthly = 200
openrouter_monthly = 30
```

The models are proposals until N1 and N2 measure them. The taxonomy and topics live in the database, edited in
Settings, with YAML import and export; [taxonomy.md](taxonomy.md) is the seed.
