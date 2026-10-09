# litwatch v1 specification

Status: draft for review, 2026-10-08. Nothing here is built. `docs/v1/plan.html` walks through
the same design interactively; where they differ, this file wins.

## 1. Why this exists

litwatch is insurance, not a reading list. Its owner follows a narrow research question and
cannot afford to miss a paper on it; everything else in the field is nice to know and mostly
arrives by other routes. Around that sits a continuously growing collection of papers that is
worth keeping for its own sake and should be usable later.

The 0.x system treats every topic the same way: seven selections, each judged per paper on one
four-tier scale by a three-model cascade. That fits the narrow topics and fails the broad ones.
Under the current rubrics, of 16,313 papers:

| Selection | Tier 1 | Tier 2 | Share at tier 1–2 |
| --- | ---: | ---: | ---: |
| cot-faithfulness | 11 | 26 | 0.2% |
| cot-monitoring | 27 | 21 | 0.3% |
| interpretability | 4 | 421 | 2.6% |
| reasoning-and-rl | 7 | 762 | 4.7% |
| alignment-and-safety | 3 | 1,001 | 6.2% |
| efficiency-and-architecture | 9 | 1,052 | 6.5% |
| agents | 38 | 2,017 | 12.6% |

28% of all papers are tier 1–2 somewhere and 6,381 have Zotero items. For a broad field,
"is this paper on topic" has thousands of true answers; the useful question is "which few
mattered", and a per-paper rubric cannot answer it.

0.x also only works inside one homelab: topics are written in Notion, n8n triggers the runs and
writes the pages, Zotero and Nextcloud hold the output. v1 is a system someone else can run.

### Goals

1. Never silently miss a paper on a watched research question, and be able to show that.
2. Give a weekly, ranked view of the fields around it, at a fraction of today's cost and noise.
3. Keep collecting every paper in scope, independent of any judgement.
4. Be complete in itself: one deployment with its own frontend, a model API key, nothing else.
5. Spend subscription credits before real dollars, by configuration, without one lab's models
   judging alone where a miss matters.

### Not in v1

Multiple users or accounts. Backfill sweeps over the stored collection. Citation counts and
citation graphs. Sources other than arXiv. Mobile apps.

## 2. Concepts

**Paper.** One arXiv paper: metadata, abstract, revisions, optionally fetched body text. Every
paper in the configured categories is stored. This is the collection.

**Topic.** Something the owner follows. It replaces the 0.x *selection* and has a **kind** that
decides the service it gets:

| | Watch | Radar | Pulse |
| --- | --- | --- | --- |
| Meant for | The research question | Fields that feed it | General awareness |
| Today's examples | CoT faithfulness, CoT monitoring | Interpretability, safety | Agents, efficiency, reasoning and RL |
| Question asked | Is this paper about my question, and how central? | What happened this week that I would care about? | What is going on? |
| Unit of output | A graded verdict per paper | About 8 picks and themes per week | A paragraph and up to 3 picks per week |
| Optimised for | Recall, then precision | Ranking | Low cost |
| Written by the owner | Subject, rubric, keywords | Subject, interests | Subject |

**Triage.** One cheap model call per paper that answers, for every active topic at once: for a
Watch topic, *could this plausibly belong* (generous, yes or no, one sentence why); for a Radar
or Pulse topic, *is it in this field* (yes or no). No tiers and no confidence field.

**Verdict.** A Watch topic's judgement of one candidate paper, on a named grade:

| Grade | Meaning | 0.x tier |
| --- | --- | --- |
| `must_read` | Central to the question and meets the rubric | 1 |
| `relevant` | On the question, worth the time | 2 |
| `adjacent` | Same neighbourhood, a source of ideas | 3 |
| `none` | Not about it | 4 |

Two models from different labs grade every candidate independently. The verdict records both.
When they agree, that is the grade. When they differ, the more inclusive grade stands and the
verdict is marked **split**, so the inbox shows it as something a person should settle.

**Review.** A weekly piece of writing for one Radar or Pulse topic: themes, and picks with a
sentence each on why. Produced by one strong model reading that week's member papers together.

**Briefing.** What the owner reads. The daily briefing lists new Watch verdicts at `relevant` or
above. The weekly briefing carries each Radar and Pulse review and a Watch recap.

**Feedback.** The owner's corrections, and the only ground truth the system has:

- a **label** on a paper for a Watch topic: relevant or not relevant, with an optional grade;
- a **miss**: a paper the owner found elsewhere that litwatch did not surface. Reporting one
  stores it, runs it through triage and grading, and records what each stage said and why.

**Library.** Papers marked saved: every Watch paper at `relevant` or above, and anything the
owner saves by hand, such as a Radar pick. The library is what exports and integrations act on.

**Publication.** The published record of a paper, when one exists: venue, year, DOI, DBLP key.
BibTeX is generated from it, or from arXiv metadata when there is none.

## 3. What happens to a paper

Nightly, in order. Each step is a job that can also run alone or over an arbitrary set of
papers, which is what backfill will use later.

1. **Intake.** Fetch new and revised papers for the configured arXiv categories and store them.
2. **Triage.** One call per untriaged paper. Stores field memberships and Watch candidates.
3. **Keyword audit.** A paper matching a Watch topic's keyword patterns becomes a candidate even
   if triage said no. No model is involved, so no model's blind spot applies.
4. **Grading.** For every (paper, Watch topic) candidate, the judge and the check model each
   return a grade and reason. The two are reconciled into a verdict.
5. **Library.** Verdicts at `relevant` or above mark the paper saved.
6. **Events.** `paper.saved`, `verdict.recorded` and similar are written to the outbox.
   Integrations consume them on their own schedule; a failing integration delays nothing.
7. **Daily briefing.** Built when there are new verdicts at `relevant` or above.

Weekly: one **review** per Radar and Pulse topic over the week's members, then the weekly
briefing.

Continuously: **publication lookup** for library papers, first when saved, then monthly for two
years, and on demand when BibTeX is exported.

On demand: the owner labels a paper, reports a miss, saves a pick, or edits a topic. Editing a
Watch topic re-grades its existing candidates and re-triages a recent window in the background.
Editing a Radar or Pulse topic affects the next review only.

### Volumes, from the 0.x journal

About 600–700 new papers a night. Watch candidates under today's rubrics are roughly 3% of
papers (anything at tier 3 or better on the two CoT selections is 5.5%, which is an upper bound
for a generous triage): 20–40 candidates a night, two grading calls each. A broad field has up to
about 500 member papers a week, which fits in one review call.

## 4. Architecture

One Python service, one Postgres database, one web app served by that service. The existing
`deep` service (deep cards, TypeScript, Pi) stays a separate optional container, unchanged.

```
web/                 React app, built to static files and served by the service
src/litwatch/
  core/              domain types and pure rules; no I/O
  store/             Postgres: migrations and one repository per aggregate
  intake/            arXiv sources → papers
  llm/               model gateway: one request shape, several providers, cost journal
  screening/         triage and the keyword audit
  watch/             grading, reconciliation, re-grading after a topic edit
  review/            weekly reviews and briefings
  publications/      published-version lookup, BibTeX
  feedback/          labels, misses, evaluation
  events/            outbox and the integration contract
  integrations/      zotero, webdav, archive, deep, webhook
  jobs/              job definitions, scheduler, locks, run records
  api/               HTTP API; serves web/
  cli.py             the same jobs and queries from a terminal
deep/                unchanged
```

Dependencies point one way: `core` ← `store` ← pipeline packages (`intake`, `screening`,
`watch`, `review`, `publications`, `feedback`) ← `jobs` ← `api` and `cli`. `llm` and `events` are
used by pipeline packages and know nothing about them. `integrations` depend on `events`,
`store` and `core` only. Nothing imports `api`.

### 4.1 Modules

**core.** `Paper`, `Topic`, `TopicKind`, `Grade`, `Opinion`, `Verdict`, `Membership`, `Review`,
`Briefing`, `Label`, `Miss`, `Publication`, `Event`. Pure functions: `reconcile(judge, check)`,
topic content hashing, grade ordering. Imports nothing from the rest.

**store.** Numbered SQL migrations applied at start-up, replacing 0.x's single idempotent
`schema.sql`. Repositories: `papers`, `topics`, `triage`, `verdicts`, `reviews`, `briefings`,
`feedback`, `publications`, `outbox`, `runs`, `calls`. A repository returns `core` types. No SQL
outside this package.

**intake.** `PaperSource` protocol with the existing OAI-PMH implementation, arXiv pacing, and
revision handling carried over from 0.x. Emits `paper.added` and `paper.revised`.

**llm.** The seam between litwatch and model providers.

- `Request`: system text, user text, output schema, effort, output-token cap.
- `Reply`: text, stop reason (`complete`, `truncated`, `refused` with a category), `Usage`.
- `Provider` protocol: `complete(model, request) -> Reply`. Two implementations: `openrouter`
  (with per-model provider lists) and `anthropic` (prompt caching, cost from a price table).
- `Gateway.complete(role, request, *, paper_id)` resolves a **role** to a provider, model and
  effort from configuration, applies retries and the timeout, journals the call with its cost,
  and reuses an identical earlier request. Roles: `triage`, `judge`, `check`, `review`.

Pipeline code names roles, never models. Moving a role between labs is a configuration change.

**screening.** The triage prompt and its schema, built from the active topics. `triage(papers)`
writes memberships and candidates. `audit(papers)` adds keyword candidates. Both are pure
functions of the papers and topic versions they are given.

**watch.** The grading prompt, one topic and one paper per call, with the rubric in context.
`grade(candidates)` asks `judge` and `check` concurrently and stores the reconciled verdict.
`regrade(topic)` handles a topic edit.

**review.** `weekly_review(topic, week)` for Radar and Pulse; a field too large for one call is
shortlisted in chunks first. `daily_briefing(day)` and `weekly_briefing(week)` assemble stored
verdicts and reviews into structured briefings; the web app and exports render them.

**publications.** `PublicationSource` protocol with a DBLP implementation over its SPARQL
endpoint: find the preprint record by arXiv DOI, then records sharing its title and an author,
and prefer the one that is not CoRR. `bibtex(paper)` renders either record. A wrong or missing
match can be corrected by hand and is then never overwritten.

**feedback.** Stores labels and misses. `replay(config)` runs every labelled or missed paper
through the current triage and grading and reports, per Watch topic: triage recall, grading
recall and precision at `relevant`, and each miss that is still missed. This is the evaluation;
there is no separate label file.

**events.** A transactional outbox: a pipeline step writes its event in the same transaction as
its data. `Integration` protocol: `name`, `wants(event)`, `handle(event)`, idempotent. A
dispatcher delivers per integration with its own cursor, retries with backoff, and exposes
failures to the API.

**integrations.** Each is off unless configured.

| Integration | Reacts to | Does |
| --- | --- | --- |
| `zotero` | `paper.saved`, `verdict.recorded`, `publication.found`, `card.ready` | Keeps a Zotero item, tags and notes in step |
| `webdav` | `paper.saved` at `must_read` | Stores the PDF for Zotero's file sync |
| `archive` | `paper.added`, `paper.revised` | Registers the paper with arxiv-archive; serves PDFs and full-text search |
| `deep` | `verdict.recorded` at `must_read` | Requests a deep card; emits `card.ready` |
| `webhook` | any, by filter | POSTs the event to a URL; n8n and the Notion export hang off this |

Notion is not a built-in integration: a documented n8n workflow, shipped as a file under
`integrations/n8n/`, subscribes by webhook and reads the API.

Integrations are the extension system, and the built-in ones get no private access. Each is one
module that implements the `Integration` protocol and may use only `events`, `store` and `core`:
it reads events, reads papers and verdicts through the repositories, keeps its own state in its
own table, and may emit events of its own (`deep` emits `card.ready`). Deep cards, Zotero and
the archive are therefore removable, and a new target (Notion directly, Slack, a reading-list
app) is one new module with no change to the pipeline. The config names which integrations to
load, by short name for the built-in ones or by dotted Python path for one kept outside the
repository. `docs/integrations.md` documents the contract, the event payloads and a worked
example, written so that a person or a coding agent can add one from that page alone.

**jobs.** One definition per job in section 3, each taking an explicit set of papers or a time
window. An in-process scheduler runs them from cron expressions in the configuration, with a
database lock so two instances never overlap. Every execution is a `run` record with counts,
cost and errors.

**api.** JSON over HTTP, described by an OpenAPI schema from which the web app's client types
are generated. Resource groups: `/papers` (get, search, BibTeX), `/topics` (CRUD, preview
against recent papers), `/inbox` (Watch verdicts needing attention), `/briefings`, `/feedback`
(labels, misses, evaluation), `/library`, `/runs`, `/integrations` (status, failures),
`/events` (changes feed), `/settings` (read-only view of configuration). A single shared token
protects it; there are no accounts.

**web.** React, TypeScript and Vite; TanStack Router and Query; Tailwind with shadcn/ui
components, which are copied into the repository and therefore restyled freely. Pages:

| Page | Purpose |
| --- | --- |
| Inbox | New Watch verdicts and splits; label with one keystroke |
| Briefings | Daily and weekly, with history |
| Paper | Abstract, verdicts with both opinions, links (arXiv, PDF, published version), BibTeX, deep card, save |
| Search | The whole collection, filtered by topic, grade, date |
| Topics | Create and edit; preview a draft against the last week before saving |
| Report a miss | Paste an arXiv link; see what each stage said |
| Quality | Recall and precision per Watch topic over time; open misses |
| System | Runs, cost by role and provider, integration health |

### 4.2 Seams

The places a part can be replaced or tested alone:

| Seam | Contract | Why it is a seam |
| --- | --- | --- |
| `PaperSource` | yields papers for a window | Other sources later; tests feed fixtures |
| `Provider` / `Gateway` | `Request` → `Reply` | Models and labs change; every stage is tested with a fake |
| `PublicationSource` | arXiv id → publication or none | DBLP access is the least stable dependency |
| `Integration` | handles an event idempotently | Everything outside the core lives behind it |
| Repositories | `core` types in and out | The only code that knows SQL |
| HTTP API | OpenAPI schema | The web app, the CLI's remote mode and n8n are equal clients |
| Job | paper set or window in, run record out | The scheduler, the CLI and backfill call the same thing |

### 4.3 Data

New or reshaped tables: `topics` (kind, subject, rubric, interests, keywords, version, status),
`memberships` (paper, topic, triage version), `candidates` (paper, topic, source: triage or
keyword, reason), `verdicts` (paper, topic, grade, split, judge and check opinions, topic
version), `reviews`, `briefings`, `labels`, `misses`, `publications`, `library`, `outbox`,
`integration_cursors`. Kept as they are: `papers`, `runs`, `inference_calls`, intake checkpoints.

### 4.4 Configuration

One `litwatch.toml` for everything that is not a secret, and environment variables or secret
files for keys. Topics live in the database and are edited in the web app, with YAML import and
export.

```toml
[intake]
categories = ["cs.CL", "cs.AI", "cs.LG"]

[models.triage]
provider = "anthropic"
model = "claude-haiku-5-5"
effort = "medium"

[models.judge]
provider = "anthropic"
model = "claude-opus-5-5"
effort = "medium"

[models.check]
provider = "openrouter"
model = "openai/gpt-5.6-luna"
effort = "medium"

[models.review]
provider = "anthropic"
model = "claude-opus-5-5"
effort = "medium"

[schedule]
nightly = "0 6 * * *"
weekly = "0 7 * * 1"

[integrations.zotero]
enabled = false
```

The models above are proposed defaults, not decisions. Each is settled by the evaluation in
milestone 2.

## 5. Models, cost and lab diversity

Different labs matter where one model's error would be final. That is true in two places:

- **Grading.** `judge` and `check` must come from different providers; start-up refuses a
  configuration where they do not.
- **Triage.** A paper triage drops is seen by no one. Three things stand behind it: the keyword
  audit, the miss channel, and the triage recall number on the Quality page. Triage is asked a
  generous yes-or-no question precisely so that a cheap model can be trusted with it.

Reviews are ranking and prose, read by a person, with no second model.

Expected spend with the proposed defaults, at about 650 papers a night: triage on credits, about
$0.25–0.40 a night; grading about 30 candidates twice, a few cents on each side; four or five
weekly reviews at roughly $0.50–1 each on credits. Real-dollar spend is the check model alone,
on the order of $1 a month, against about $30 a month in 0.x. These are estimates from 0.x token
counts and must be re-measured in milestone 2.

### Findings carried from the 0.14 Anthropic branch

Branch `feat/anthropic-backend` (commit 7911f79) is reference, not for merging. Full report:
`evals/2026-10-08-haiku-primary.md`.

- A neutral request and reply with per-provider adapters is the right shape; 0.x bolted Claude
  on behind OpenRouter's request format to avoid touching forty tests. v1 starts neutral.
- Anthropic returns tokens and no charge, so cost needs a price table, including Haiku 5.5's
  higher rate above 100k prompt tokens and per-model cache-read rates.
- Prompt caching worked for the screening prompt on Haiku 5.5: about 4,500 of 4,900 input
  tokens were read from cache. Claude counts about 1.7 times as many tokens as Luna for the
  same prompt.
- Haiku 5.5 as a tier-assigning primary was stricter than Luna at the tier 2 boundary at low
  effort and close to it at high effort. It also uses a low-confidence label Luna never uses,
  which tripled re-screens. v1's triage has neither tiers nor a confidence field, which removes
  both problems by construction; whether its recall holds is what milestone 2 measures.
- Refusals carry a category (`cyber`, `bio`, `frontier_llm`, `reasoning_extraction`,
  `general_harms`). None occurred in about 600 calls. They are journalled by category.
- Muse Spark's contributor endpoint has a long latency tail (90th percentile 47 s) and about
  2% errors; a single slow call must not stop a run.

## 6. Published versions

DBLP is the source. Its HTTP search API answered this server with a bot challenge on
2026-10-08, on all three mirrors; its SPARQL endpoint (`sparql.dblp.org`) did not, and an
exact-title query returned in about 50 ms with both the CoRR record and the conference record.
v1 uses SPARQL, behind `PublicationSource`, batched and paced, with the usage terms checked
before release. If that access closes, the fallback is a local index built from DBLP's monthly
data dump.

Matching: locate the preprint record by its arXiv DOI; take DBLP's own title and author list
from it; find other records with that title and at least one shared author; choose the
non-CoRR one, the earliest if several. A paper retitled at publication is not found this way;
the Paper page lets the owner paste a DOI or DBLP key instead.

## 7. Moving from 0.14

- **Papers and the inference journal** are kept as they are.
- **Topics.** The two CoT selections become Watch topics with their rubrics. Interpretability
  and alignment-and-safety become Radar. Agents, efficiency-and-architecture and
  reasoning-and-rl become Pulse.
- **Old verdicts.** For Watch topics, tiers map to grades and are kept as verdicts with a
  `migrated` marker and a single opinion. For Radar and Pulse topics, tier 3 or better becomes
  a membership and the tier is dropped.
- **Zotero.** Watch items stay. The several thousand items that exist only because of a broad
  selection's tier 2 are the open question in section 9.
- **Notion and n8n.** The current workflows keep working against 0.14 until cutover, then are
  replaced by the webhook workflow.
- **Cutover.** v1 runs beside 0.14 on a copy of the database for at least a week. The switch
  happens when v1's Watch output contains everything 0.14 put at tier 1–2 on the CoT selections
  that week, or each difference has been looked at.

## 8. Milestones

Each ends with something that runs and a stated check. Sizes are relative, not dates.

**M0 · Skeleton.** New package layout, migrations, `litwatch.toml`, `core` types, `store`
repositories for papers, `intake` ported, CI. *Check:* a fresh database ingests a night of arXiv;
0.x intake tests pass against the new modules.

**M1 · Model gateway.** `llm` with both providers, roles, journal, retries, refusal handling,
price table. *Check:* the same request runs through either provider by changing one line of
configuration; journal cost matches hand calculation on recorded responses.

**M2 · Watch pipeline and its evaluation.** Topics with kinds, triage, keyword audit, grading
with two labs, reconciliation, `feedback.replay`. Seed labels from `evals/labeled.yaml` and from
0.14's CoT verdicts. Trial the triage and grading defaults. *Check:* triage recall on labelled
Watch positives is at least 0.14's; the model defaults in section 4.4 are confirmed or changed,
with the numbers written to `evals/`.

**M3 · API and web app, Watch first.** Starts with a design pass: the screens are discussed
and a few sketches compared before any of them is built. Then the API with generated client; Inbox, Paper, Search, Topics,
Report a miss, Quality. Labels and misses flow into the evaluation. *Check:* a day's Watch
verdicts can be read, labelled and exported as BibTeX without touching Notion or Zotero; a
reported miss shows what each stage said.

**M4 · Reviews and briefings.** Radar and Pulse reviews, daily and weekly briefings, Briefings
page, in-process scheduler, System page. *Check:* two consecutive weekly briefings generated on
schedule; the owner prefers them to 0.14's digests or says what is missing.

**M5 · Published versions.** `publications` with DBLP, monthly re-check, manual override,
BibTeX from the published record. *Check:* on 50 library papers known to be published, the
lookup finds the right record for at least 45 and never a wrong one.

**M6 · Integrations.** Outbox and dispatcher; Zotero and WebDAV, archive, deep, webhook; the n8n
workflow for Notion. *Check:* with every integration disabled the system is fully usable; with
each enabled, its 0.14 behaviour is reproduced for Watch papers; a small new integration can be
written from `docs/integrations.md` without editing any other package.

**M7 · Migration and cutover.** Data migration, a week side by side, switch production.
*Check:* the cutover rule in section 7.

**M8 · Release.** Compose file, install and configuration docs, repository clean-up, MIT
licence, `v1.0.0`.

After v1: backfill sweeps over the collection for Watch topics; citation counts; other sources.

## 9. Open questions

1. **Zotero items from broad selections.** Leave the existing few thousand in place, or remove
   the ones litwatch created and nothing else touched? Removal is destructive and needs an
   explicit yes.
2. **Check model.** Luna at medium effort is proposed because it is cheap and already measured;
   Muse is the alternative with a third lab. Milestone 2 can test both.
3. **Radar picks and the library.** Proposed: picks are not saved automatically; the owner saves
   the ones that matter.
4. **Search.** Proposed: litwatch searches titles and abstracts in its own database; full-text
   search and PDFs come from arxiv-archive when that integration is on. The archive's existing
   web interface would then be redundant for this purpose.
5. **Reporting a miss from a phone.** If most misses are seen on a phone, a share-sheet
   shortcut that posts to the API is worth adding in M3.
6. **The PI's view.** Whether the Notion export should carry only Watch topics.
