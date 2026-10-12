# litwatch v1 specification

Status: milestones 0 to 2 are built on branch `v1` (2026-10-09); the rest is not. `docs/v1/plan.html` walks through
the same design interactively, and `docs/v1/design.html` with `docs/v1/DESIGN.md` carry the web app's design (agreed
2026-10-11); where they differ, this file wins.

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

Multiple users or accounts. Backfilling papers litwatch never ingested, such as the archive's older years, and anything
built to make bulk re-runs cheap (embedding preselection, batch pricing). A re-run over a
date range of stored papers is in v1, because it is only the ordinary job with a window. Citation counts and
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
| Review cadence | None; verdicts arrive nightly | Weekly, or daily if the topic says so | Weekly |
| Written by the owner | Subject, rubric, keywords | Subject, interests | Subject |

**Triage.** One cheap model call per paper that answers, for every active topic at once: for a
Watch topic, *could this plausibly belong* (generous, one sentence why); for a Radar
or Pulse topic, *is it in this field* (yes or no). No tiers and no confidence field. For a
Watch topic the model chooses between about, related and unrelated, and only unrelated stops
the paper; what is stored is a yes or a no. Asked a plain yes or no, the triage model judged
membership and dropped adjacent papers (`evals/2026-10-09-v1-watch.md`).

**Verdict.** A Watch topic's judgement of one candidate paper, on a named grade:

| Grade | Meaning | 0.x tier |
| --- | --- | --- |
| `must_read` | Central to the question and meets the rubric | 1 |
| `relevant` | On the question, worth the time | 2 |
| `adjacent` | Same neighbourhood, a source of ideas | 3 |
| `none` | Not about it | 4 |

Two models from different labs grade every candidate independently. The verdict records both.
When they agree, that is the grade. When they differ, the more inclusive grade stands and the
verdict is marked **split**. A split is counted on the Digest's status line and listed on the Quality page, where the
owner may settle it; the reading surfaces show the paper at the standing grade with no mark.

**Review.** A piece of writing for one Radar or Pulse topic: themes, and picks with a
sentence each on why. Produced by one strong model reading the period's member papers together.
The period is a week unless the topic's `cadence` is `daily`, which is meant for a Radar field
the owner follows closely (interpretability today). A daily review is shorter: up to 5 picks
and a line on anything that stands out, with no themes forced onto one day's papers.

**Take.** Litwatch's own words on one paper for one Watch topic: what the paper does and why it matters for the
question, written for the owner by the writer (section 3) from the abstract, the topic's subject, and whatever opinions
exist for that paper and topic: both graders' for a production verdict, the evaluation's for a miss the owner marked
relevant, the single migrated opinion for a 0.14 positive, none for a paper the owner labelled relevant out of nowhere.
Two or three sentences for `must_read`, one for `relevant`. A take is its own row, keyed by paper, topic, paper
revision and topic version, so it is bound to the text and rubric it was written from. It is the only text about a
paper the reading surfaces show besides the abstract and a deep card; grade names, model names and the graders' reasons
appear in two places only, the status line's split count and the folded "how this was chosen" on a paper page, and
otherwise stay behind Settings.

**Issue.** What the owner reads, written like a newspaper. The daily issue opens with a lede, then one section per
Watch topic with the papers newly at an effective grade of `relevant` or above and their takes, then the review of
every daily topic. The weekly issue, on Monday, carries each weekly Radar and Pulse review and a Watch recap: per
Watch topic the week's papers newly in the library, those the owner set aside, and the misses reported that week. (These were
called briefings in earlier drafts; the API keeps the shape, the name follows the page.)

**Feedback.** The owner's corrections, and the only ground truth the system has. Every reading surface offers it and
none asks for it: there is no queue of papers to label there. Only Calibrate, behind the gear on the Quality page,
asks, because the owner opens it to answer (decision 20).

- a **label** on a paper for a Watch topic: relevant, adjacent, not relevant or unsure, with an
  optional grade. Unsure changes nothing and is left out of every metric.
  Not relevant means an effective grade of `none`. Adjacent means `adjacent`: a source of
  ideas for the topic without being about it, which is not a positive for recall. Relevant without a grade keeps the
  verdict's grade if that is `relevant` or above, and is `relevant` otherwise. A label is given explicitly: in the
  label row on a paper page, in a Calibrate session on the Quality page, or by marking a miss. It records how its
  paper came to be labelled (its origin, section 4.1) and keeps that origin when the owner later changes its value,
  so a change of mind never moves a paper out of the sample it was drawn in;
- a **miss**: a paper the owner found elsewhere that litwatch did not surface. Reporting one
  takes only the arXiv link, so it works from a phone's share sheet. It stores the paper, runs
  triage, grades it against every Watch topic whatever triage said (there are few), and
  records what each stage said and why. The owner then marks which topic it belonged to. That mark
  is a label of relevant for the topic, so it saves the paper like any other label, and the
  evaluation replays it for that topic;
- a **reaction**: a thumb up or down on a paper for the topic it was shown under, or for no topic, one per paper and
  topic, replaced on change and removable. On a thumb down a reason: not for me, unrelated to the topic, already knew it, overrated; on a thumb up,
  optionally, great find. A reaction is never a label and writes none. On a Watch topic two reasons are nevertheless
  the owner's word on the paper's place in that topic: "unrelated to the topic" counts as not relevant and "great
  find" as relevant without a grade. The other reasons, and every reaction on a Radar or Pulse field or on no topic,
  have no effect beyond being collected (decision 17). Clearing or changing the reaction takes back its word and
  nothing else. The acknowledgement says what happened ("Marked not relevant for CoT monitoring; it leaves the
  library"). Reactions feed the Quality page as a **correction rate**:
  per Watch topic and trailing 30 days, the share of papers shown in issues at `relevant` or above that carry a thumb
  down with reason not for me, unrelated or overrated; "already knew it" is not a correction and is not counted.
  Reactions are biased by what the owner chose to react to, so the evaluation reads labels only, and precision is
  measured from a drawn calibration sample (section 8, M3).
- an **appraisal**: the owner's view of a paper regardless of topic (matters, fine, not for me) with an optional
  note, and an **accurate or off** on a take. Both are given in a Calibrate session only. A paper that matters
  to the owner but fits no Watch topic is the signal that a topic is missing or a rubric is drawn too narrowly;
  Quality lists those papers with their notes, for the owner or an agent to read.

**The owner's word** on a paper for a Watch topic is the newer of its label and its word-carrying reaction; an
unsure label is no word, and a provisional label (accepted in bulk, section 8, M2) is older than either. The
**effective grade** is the owner's word where there is one, otherwise the current verdict (section 3, step 5). Every
reader-facing surface uses it: issues, topic pages, the library, exports and integrations.

**Library.** Papers marked saved: every paper at an effective grade of `relevant` or above for a Watch topic, and
anything the owner saves by hand, such as a Radar pick, except what the owner has unsaved. Save and Unsave are one
toggle on every paper, and the owner's newest use of it outranks every other reason: an unsaved paper leaves the
library and stays out, even if a later re-grade would save it, until the owner saves it again. Unsaving is not a
label: the paper keeps its place in its topics and in past issues. The library is what integrations act on; BibTeX
can be copied for any paper.

**Publication.** The published record of a paper, when one exists: venue, year, DOI, DBLP key.
BibTeX is generated from it, or from arXiv metadata when there is none.

## 3. What happens to a paper

Nightly, in order. Each step is a job that can also run alone or over an arbitrary set of
papers, which is what backfill will use later.

1. **Intake.** Fetch new and revised papers for the configured arXiv categories and store them.
2. **Triage.** One call per untriaged paper. Stores field memberships and Watch candidates.
   A revision of a stored paper is untriaged again only when its title or abstract was
   rewritten (similarity below 0.8, the 0.14 rule); otherwise it keeps what it has. Each
   rewrite raises the paper's own **revision** number. arXiv's version number cannot do this
   job, because it is often unknown, both before and after a rewrite. Every candidate and
   verdict records the paper revision it was made from (and the arXiv version, for
   reference), a candidate whose paper was rewritten is graded again, and a re-screened
   paper's earlier verdict is kept as history, not overwritten. A rewrite whose earlier
   text holds a verdict above `none` is graded again even when neither triage nor a keyword
   puts the new text forward, so that a verdict on text that no longer exists cannot stay
   current.
3. **Keyword audit.** Every paper that triage did not make a candidate for a Watch topic is
   checked against that topic's keyword patterns, whether or not it joined a Radar or Pulse
   field, and becomes a candidate on a match. No model is involved, so no model's blind spot
   applies. A candidate is one row per paper, topic and topic version; a paper found by both
   paths is one candidate carrying both sources, and is graded once.
4. **Grading.** For every (paper, Watch topic) candidate, the judge and the check model each
   return a grade and reason. The two are reconciled into a verdict.
5. **Library.** An effective grade of `relevant` or above marks the paper saved. For one topic the
   owner's word (section 2) outranks the verdict. Saved state belongs to the paper, not the topic, so it
   is recomputed from every reason the paper has: an effective grade of `relevant` or above on any Watch topic,
   or a save by hand; an unsave by hand, while it is the owner's newest use of the toggle, overrides them all. The
   current verdict for a paper and topic is chosen by text first and time second: among
   the verdicts for the newest paper revision and topic version that have one, the most
   recently recorded. Older rows are history and give no reason. A re-grade that is still pending, or that failed, replaces
   nothing: the previous verdict stays current, and the paper stays saved, until a new verdict
   is recorded or the owner's word changes. `paper.unsaved` is emitted when the paper leaves the library, because
   its last reason went or because the owner unsaved it.
6. **Writer.** When a paper's effective grade for a Watch topic crosses from below `relevant` to `relevant` or above,
   whether a verdict or the owner's word moved it, the pair becomes **due**: due for a take, and due for an issue,
   unless the pair has already appeared in an issue, in which case it is due for a take only. A paper the owner set
   aside and then took back is therefore not announced again; its old entry stops being struck through. The writer
   takes every pair due for a take and writes it in litwatch's voice, addressed to the owner, from the abstract at the
   verdict's paper revision, the topic's subject at its topic version, and the opinions that exist (see *Take*); where
   no verdict exists (the owner's word alone moved the paper), the current paper revision and topic version are used. A
   failed call leaves the pair due and is retried next run; after three failed runs the pair enters the issue with its
   title and abstract and no take, and the writer keeps trying. A take is written again when the pair gets a new
   current verdict with a different paper revision, topic version or grade (a move between `relevant` and `must_read`
   changes its length); a re-grade that lands on the same grade from the same text keeps the take. Rewriting a take
   makes nothing due for an issue again. Once a night the writer also produces the issue's lede from that night's
   entries, and on Monday the weekly issue's Watch recap. One gateway role, `writer` (section 4.4), Opus 5.5 on the
   Anthropic API by default (credits), compared in M3 with one or two OpenRouter models on a small set of papers; at
   one to five papers a night the cost is cents. The review writer of Radar and Pulse topics shares its prompt
   scaffolding.
7. **Events.** `paper.saved`, `verdict.recorded`, `grade.changed`, `take.written` (paper, topic, take id) and similar
   are written to the outbox. `take.written` may arrive after `paper.saved`; an integration that mirrors notes
   (Zotero) subscribes to it and updates the note when it comes. `grade.changed` fires when a paper's effective grade for a topic changes,
   whether a verdict or the owner's word caused it.
   Integrations consume them on their own schedule; a failing integration delays nothing.
8. **Daily issue.** One per calendar date in the configured timezone (`[schedule] timezone`, default the server's),
   keyed by that date, built at the end of the nightly run from every pair due for an issue, with their takes, and the
   daily reviews. A pair is taken into the issue when its take is ready, or when the writer has failed on it three
   runs, in which case it enters with title and abstract and no take; until then it waits. Including a pair consumes
   its due state for good, so a pair appears in one issue only (step 6); a pair still waiting on its take makes the
   next issue, never a duplicate. Running the day's jobs again amends the same
   issue, adding entries and never repeating one. An issue exists when there is at least one Watch entry or one daily
   review; adjacent arrivals alone make no issue, though an issue that exists counts them in its fold line. A section
   whose review failed says so in one line and is filled in if the review is re-run that day. Issues use the effective
   grade (section 2), as every reader-facing surface does. A paper the owner rejected does not come back because a
   re-grade liked it. A night with no issue leaves the Digest on the last one, which says since when.

Weekly: one **review** per weekly Radar and Pulse topic over the week's members, then the weekly
issue. A topic with a daily cadence is reviewed after each nightly run instead, over that
night's members, before the daily issue is built.

Continuously: **publication lookup** for library papers, first when saved, then monthly for two
years, and on demand when BibTeX is exported.

On demand: the owner reacts to or labels a paper, reports a miss, saves or unsaves a paper, calibrates, asks for a
deep card, or edits a topic. Editing a
topic applies forward: the new version is used from the next run, and past papers keep the
answer they got. Every membership, candidate and verdict records the topic version that
produced it, so mixed history is visible. Nothing retroactive runs by itself. After an edit
to a Watch topic the app offers re-runs over a chosen scope, each showing its paper count and
estimated cost:

- the owner's labels and reported misses, which shows whether the edit helped;
- the topic's existing candidates, re-graded;
- papers matching the topic's keyword patterns anywhere in the collection, picked without a
  model and then triaged;
- a date range.

Editing a Radar or Pulse topic re-runs nothing; a mid-week edit gives one mixed week. The cost
of this rule is that an old paper the previous wording excluded stays unfound unless a keyword
or a paid date range reaches it.

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
  watch/             grading, reconciliation, re-grading a chosen set
  review/            reviews, the writer, issues
  publications/      published-version lookup, BibTeX
  feedback/          reactions, labels, misses, evaluation
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
`Take`, `Issue`, `Label`, `Reaction`, `Miss`, `Publication`, `Event`. Pure functions: `reconcile(judge, check)`,
topic content hashing, grade ordering. A topic's version is the SHA-256 of its kind, subject,
rubric, interests and keywords as canonical JSON (sorted keys, keywords sorted); status and
name are not part of it. A hash says whether two versions differ, not which is newer, so each
saved edit also gets the topic's next revision number, kept in `topic_versions`. Records
refer to the revision, and "newest topic version" always means the highest revision. Imports nothing from the rest.

**store.** Numbered SQL migrations applied at start-up, replacing 0.x's single idempotent
`schema.sql`. Repositories: `papers`, `topics`, `triage`, `verdicts`, `reviews`, `issues`,
`feedback`, `publications`, `outbox`, `runs`, `calls`. A repository returns `core` types. No SQL
outside this package.

**intake.** `PaperSource` protocol with the existing OAI-PMH implementation, arXiv pacing, and
revision handling carried over from 0.x. Emits `paper.added` and `paper.revised`.

**llm.** The seam between litwatch and model providers.

- `Request`: system text, user text, output schema, output-token cap. Effort is not part of
  it; it comes only from the role's configuration.
- `Reply`: text, stop reason (`complete`, `truncated`, `refused` with a category), `Usage`.
- `Provider` protocol: `complete(model, request) -> Reply`. Two implementations: `openrouter`
  (with per-model provider lists) and `anthropic` (prompt caching, cost from a price table).
- `Gateway.complete(role, request, *, paper_id)` resolves a **role** to a provider, model and
  effort from configuration, applies retries and the timeout, journals the call with its cost,
  and reuses an identical earlier call. Identical means the same resolved provider, model,
  effort and routing as well as the same `Request`, so `judge` and `check` never share a reply
  and a replay after a model change calls the new model. Roles: `triage`, `judge`, `check`, `review`, `writer`.

Pipeline code names roles, never models. Moving a role between labs is a configuration change.

**screening.** The triage prompt and its schema, built from the active topics. `triage(papers,
topics)` returns each paper's memberships and Watch answers; `audit(papers, topics)` returns
keyword matches. Both are pure functions that write nothing. `record(results)` is the separate
step that turns results into memberships and candidates; the nightly job calls it, and
evaluation never does.

**watch.** The grading prompt, one topic and one paper per call, with the rubric in context.
`grade(paper, topic)` asks `judge` and `check` concurrently and returns both opinions and the
reconciled grade, writing nothing; `record(result)` stores the verdict in production, and
evaluation stores the same result in its own tables. `regrade(papers, topic)` re-grades a
chosen set after a topic edit.

A paper whose triage call fails is tried again on every run, after the new papers, so it
never takes a new paper's place when a run is capped. Triage does not give up, because a
paper it drops is seen by no one. A paper that has failed three runs in a row is counted as
stuck in the run record (`triage_stuck`). Showing those papers to the owner, with a way to
dismiss one, belongs to Settings → Runs and cost (M4) and is not built yet.

Candidates are graded independently, so one failure stops nothing else. A verdict needs two
valid opinions. If either call still fails after its retries, or comes back truncated or
refused, no verdict is made: the candidate stays pending with the failure recorded, and is
tried again on the next run. After three failed runs it is no longer retried by itself and
is listed on the Quality page as an ungraded candidate, with a button to grade it now, because on a Watch topic a paper must not disappear behind a provider error. The
candidate row itself carries this state (pending or graded, failed runs, last error), since
the call journal knows only the paper. A pending candidate is dropped from the retry queue
once a newer candidate exists for the same paper and topic, so a late retry of an old
version cannot overtake a newer verdict. A candidate still pending when its topic is edited
is graded under the topic as it then is, and its verdict records that revision.

**review.** `review(topic, period)` for Radar and Pulse, where the period is a week or a day; a field too large for one call is
shortlisted in chunks first. `take(paper, topic, source)` writes a paper's take, where the source is the current verdict, an evaluation
result (a miss), a migrated opinion, or the owner's word alone, and `lede(day)` the issue's opening
(section 3, writer). `daily_issue(day)` and `weekly_issue(week)` assemble stored takes, verdicts and reviews into
structured issues; the web app and exports render them.

**publications.** `PublicationSource` protocol with a DBLP implementation over its SPARQL
endpoint: find the preprint record by arXiv DOI, then records sharing its title and an author,
and prefer the one that is not CoRR. `bibtex(paper)` renders either record. A wrong or missing
match can be corrected by hand and is then never overwritten.

**feedback.** Stores labels, reactions, appraisals, take ratings, saves and unsaves, and misses, and resolves the
owner's word and the effective grade (section 2) for the rest of the system. Replay, topic previews and the grading of a reported
miss are dry runs: they call the same pure triage, audit and grading functions but write
their answers to evaluation tables only, never to candidates, verdicts, the library or the
outbox, so running an evaluation cannot save a paper or wake an integration. Their model
calls are journalled like any other. `replay(config)` runs every labelled or missed paper
through the current triage and keyword audit, then grades all of them whether or not they
became candidates, as the miss flow does. Per Watch topic it reports four numbers over the
papers the owner labelled relevant. The two that involve grading are over those sent to
grading, which is all of them unless a run grades a subset; a paper whose grading failed
stays in the count as a miss, so a provider outage cannot raise recall. The four are: triage recall (the triage model
alone said yes, before the keyword audit), candidate recall (triage or the audit made it a
candidate), grading recall (graded `relevant` or above when forced), and end-to-end recall
(a candidate and graded `relevant` or above). Triage recall is the one that judges the
triage model; the audit must not be allowed to hide its misses. It
also reports each miss still missed, and precision at `relevant`. Every label records how
its paper was chosen: calibration sample (`sample`, the random candidate sample), reject sample, 0.14 positive,
reader (the paper page's label row, a paper taken into Calibrate because of a split or a reaction, and the M2
labelling page's labels, which migrate to it), or miss. Production precision is computed from the calibration
sample alone, since only
that one was drawn the way production surfaces papers. Precision over the other groups is
reported per group and never pooled.
Grading the non-candidates is what keeps a triage miss from being counted as a grading error. This is the evaluation;
there is no separate label file.

**events.** A transactional outbox: a pipeline step writes its event in the same transaction as
its data. `Integration` protocol: `name`, `wants(event)`, `handle(event)`, idempotent. A
dispatcher delivers per integration with its own cursor, retries with backoff, and exposes
failures to the API.

**integrations.** Each is off unless configured.

| Integration | Reacts to | Does |
| --- | --- | --- |
| `zotero` | `paper.saved`, `paper.unsaved`, `grade.changed`, `take.written`, `publication.found`, `card.ready` | Keeps a Zotero item, tags and notes in step. On `paper.unsaved` it tags the item as dropped and never deletes it |
| `webdav` | `grade.changed` to `must_read` | Stores the PDF for Zotero's file sync |
| `archive` | `paper.added`, `paper.revised` | Registers the paper with arxiv-archive; serves PDFs, and answers searches over its titles and abstracts |
| `deep` | `grade.changed` to `must_read`, `card.requested` | Requests a deep card; emits `card.ready` |
| `webhook` | any, by filter | POSTs the event to a URL; n8n and the Notion export hang off this |

An integration that acts on a grade checks the paper's effective grade (section 2) when it handles
the event. A `must_read`
verdict the owner has overruled, or one a later version superseded, fetches no PDF and
requests no card.

`card.requested` is the owner asking for a deep card by hand, and it is honoured whatever the
paper's grade or topic: a `relevant` Watch paper, a Radar or Pulse pick, or a paper found by
search. Only `must_read` gets a card without being asked. The request is one button on the paper
page (and the reading pane, which is the same page), shown only while an integration that answers
`card.requested` is loaded; a card already stored is shown whether or not one still is. One API call.

Notion is not a built-in integration: a documented n8n workflow, shipped as a file under
`integrations/n8n/`, subscribes by webhook and reads the API.

Integrations are the extension system, and the built-in ones get no private access. Each is one
module that implements the `Integration` protocol and may use only `events`, `store` and `core`:
it reads events, reads papers and verdicts through the repositories, keeps its own state in its
own table, and may emit events of its own (`deep` emits `card.ready`). Deep cards, Zotero and
the archive are therefore removable, and a new target (Notion directly, Slack, a reading-list
app) is one new module with no change to the pipeline. The config names which integrations to
load, by short name for the built-in ones or by dotted Python path for one kept outside the
repository. An integration may also implement `SearchProvider`, a second, optional protocol defined in
`core`: a synchronous `search(query, filters)` returning hits keyed by arXiv id. `archive`
does. The API never imports `integrations`; the service's start-up wiring hands it the list of
loaded providers, it queries them beside its own repository with a short timeout each, and a
provider that fails or is slow is left out of that response and reported on its card under Settings → Integrations.
`docs/integrations.md` documents both contracts, the event payloads and a worked
example, written so that a person or a coding agent can add one from that page alone.

**jobs.** One definition per job in section 3, each taking an explicit set of papers or a time
window. An in-process scheduler runs them from cron expressions in the configuration, with a
database lock so two instances never overlap. Every execution is a `run` record with counts,
cost and errors.

**api.** JSON over HTTP, described by an OpenAPI schema from which the web app's client types
are generated. Resource groups: `/papers` (get, list and search with filters for topic, saved, year and
source, BibTeX for one paper or a filter), `/topics` (list with what is new, the topic's papers and reviews; CRUD,
preview against labelled papers, scoped re-runs), `/digest` (issues, daily and weekly, with sections and takes; the
status line), `/feedback` (reactions, labels, misses, evaluation, Calibrate sessions and their answers, the
"matters, no topic" list), `/library` (the saved set; save and unsave),
`/runs`, `/integrations` (status: description, enabled, cursor lag, failures with the last error, retry and skip, search
provider statistics), `/events` (changes feed), `/settings` (read-only view of configuration). A single shared token
protects it; there are no accounts.

**web.** React, TypeScript and Vite; TanStack Router and Query; Tailwind with shadcn/ui
components, which are copied into the repository and therefore restyled freely. The web app is the user side of
litwatch: where the value is handed over, read like a morning newspaper, with the pipeline behind a settings gear. Its
design is `docs/v1/DESIGN.md` (principles, tokens, anatomy) and `docs/v1/design.html` (the drawings). Navigation is
three places and a gear: Digest, Topics, Papers, as a footer on the phone and three words in the header on a desktop.

| Page | Purpose |
| --- | --- |
| Digest | A feed of issues, newest open, earlier ones folded to date and lede. A masthead with the **status line**, which reads the latest nightly run: red when that run failed or none finished in the last 36 hours ("last night's run failed · showing Tuesday"); amber when it finished but left something for Quality, with a count: split verdicts among the papers shown (section 9, decision 14), candidates still ungraded, a review that failed; green otherwise ("530 read · all quiet", the count being papers triaged in that run); "running since 06:02" while it runs; "no run yet" on a fresh install. It opens Quality, whose top shows the night's splits, ungraded candidates and a failure summary linking to the run. Opening an old issue does not change it. The most recent weekly issue is pinned as a row above the feed, labelled with its week, absent until one exists. On a day with no issue the last one is shown with "nothing new since"; before any issue exists the Digest says when the first run is |
| Paper | Take, abstract, deep card when one is stored, else a request button while a deep-card integration is loaded, else nothing; links (arXiv, PDF, published version), BibTeX, save or unsave, history (versions seen, and when the paper became a candidate or was graded for each Watch topic); thumbs; "how this was chosen" folded at the end with the triage answer, both opinions by model name, the reconciled grade and an optional label row (origin `reader`) |
| Topics | One screen of topic cards grouped Watch, Radar, Pulse, each with its colour, kind, one line of what is new (entries in issues since the topic was last opened, kept server-side in `topic_visits`) and counts; on a desktop a rail with the chosen topic open beside it. A topic page is the papers table with the topic fixed: by default the papers at an effective grade of `relevant` or above, adjacent on a toggle, nothing below that; for Radar and Pulse the picks, with the reviews above the table. The gear opens the editor in Settings |
| Papers | The whole collection as one sortable table (title with authors, published, added, take of the first Watch topic by the owner's order, saved, links; a grade dot per row, an empty ring for a paper that is no topic's candidate, "grading" for a pending one, the take column empty where there is none) with a search box and filters for topic, saved, year and source; the archive's hits merged when it is connected; fetch an archive-only paper; report a miss from a row or by arXiv id; export BibTeX for the filter or selection. Saved is the filter that makes it the library |
| Settings | Topics (create, edit, order, pause; preview a draft against labelled papers and misses; scoped re-runs after saving), Quality (last night's splits with both opinions and the label row, ungraded candidates, failures; **Calibrate**, a session for giving the owner's word, section 8 M3; recall and precision per Watch topic over evaluation runs; correction rate; the "matters, no topic" list with its notes), Misses (reported misses with what each stage said), Runs and cost, Integrations (one card per module), Configuration (read-only) |

Phone first, desktop whole: one column on the phone; on a desktop the Digest gets a reading pane beside the feed and
Topics and Papers are real tables. With no integration connected every page works: search is litwatch's own, PDFs link
to arXiv, published versions come from DBLP in the core, and the deep card section is absent unless a card is stored.
The shared token authorises every call, reads and writes alike: labels, reactions, misses, saves, topic edits and
re-runs, integration retry and skip, card requests. Only the configuration is out of its reach (section 9, decision 11).

### 4.2 Seams

The places a part can be replaced or tested alone:

| Seam | Contract | Why it is a seam |
| --- | --- | --- |
| `PaperSource` | yields papers for a window | Other sources later; tests feed fixtures |
| `Provider` / `Gateway` | `Request` → `Reply` | Models and labs change; every stage is tested with a fake |
| `PublicationSource` | arXiv id → publication or none | DBLP access is the least stable dependency |
| `Integration` | handles an event idempotently | Everything outside the core lives behind it |
| `SearchProvider` | query and filters → hits keyed by arXiv id | Lets an integration add results to search without the API knowing which one |
| Repositories | `core` types in and out | The only code that knows SQL |
| HTTP API | OpenAPI schema | The web app and n8n are equal clients |
| Job | paper set or window in, run record out | The scheduler, the CLI and backfill call the same thing |

### 4.3 Data

New or reshaped tables: `topics` (kind, subject, rubric, interests, keywords, version, status, colour, position),
`topic_versions` (topic, revision, hash, the content at that revision, saved at),
`memberships` (paper, topic, topic version, paper version), `candidates` (paper, topic,
topic version, paper version, paper revision, sources: triage, keyword or both, reason, state, failed runs,
last error), `verdicts` (paper, topic, topic version,
paper version, paper revision, grade, split, judge and check opinions, recorded at; each grading is its own
row with its own id, so a re-run over the same versions adds a row, and the latest recorded
is current), `takes` (paper, topic, paper revision, topic version, source: verdict, evaluation, migrated or label,
text, written at, failed runs), `due` (paper, topic, due for take, due for issue), `reviews`, `issues` (date, kind
daily or weekly, lede, entries as paper, topic and take id in section order, so a rewritten take never replaces the
one an old issue showed; review references), `labels` (paper, topic, value, grade, origin, provisional, at),
`reactions` (paper, topic, value, reason, at), `appraisals` (paper, value: matters, fine or not for me, note, at),
`take_ratings` (take, accurate or off, at), `calibration_sessions` (drawn at, seed, the papers in order and why each
was drawn), `topic_visits` (topic, last opened at), `misses`, `publications`, `library` (paper, the owner's newest
save or unsave, at), `outbox`,
`integration_cursors`, `eval_runs` (when, the configuration and models used, topic versions)
and `eval_results` (run, paper, paper version, topic, triage answer, keyword match, both
opinions, reconciled grade, grading error), which hold replays, previews and miss traces. `papers` gains `revision`. Kept as they are: `runs`, `inference_calls`, intake checkpoints.

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
model = "meta/muse-spark-1.3-contributor"
effort = "high"

[models.review]
provider = "anthropic"
model = "claude-opus-5-5"
effort = "medium"

[models.writer]
provider = "anthropic"
model = "claude-opus-5-5"
effort = "medium"

[schedule]
nightly = "0 6 * * *"
weekly = "0 7 * * 1"
timezone = "America/Denver"

[integrations.zotero]
enabled = false
```

The models above are proposed defaults, not decisions. Triage, judge and check were settled by the evaluation in
milestone 2; the writer is Opus 5.5 unless the M3 comparison says otherwise; review is settled in M4.

## 5. Models, cost and lab diversity

Different labs matter where one model's error would be final. That is true in two places:

- **Grading.** `judge` and `check` must come from different labs; start-up refuses a
  configuration where they do not. The lab is the organisation that trained the model, not the
  account it is reached through: it is read from the OpenRouter model prefix (`openai/`,
  `z-ai/`, …) and is `anthropic` for the direct API. One OpenRouter key therefore satisfies
  the rule on its own.
- **Triage.** A paper triage drops is seen by no one. Three things stand behind it: the keyword
  audit, the miss channel, and the triage recall number on the Quality page. Triage is asked a
  generous yes-or-no question precisely so that a cheap model can be trusted with it.

Reviews are ranking and prose, read by a person, with no second model.

Expected spend with the proposed defaults, at about 650 papers a night: triage on credits, about
$0.25–0.40 a night; grading about 30 candidates twice, a few cents on each side; four or five
weekly reviews at roughly $0.50–1 each on credits, and a daily review for each topic that asks
for one, not yet measured and also on credits; takes, ledes and recaps for one to five papers a
night, cents, on credits. Real-dollar spend is the check model alone,
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
  `migrated` marker and a single opinion. Migration writes a take for each migrated positive from that opinion in
  one batch (source `migrated`, about 80 papers), and makes none of them due for an issue. For Radar and Pulse topics, tier 3 or better becomes
  a membership and the tier is dropped.
- **Zotero.** Watch items stay. The several thousand items that exist only because of a broad
  selection's tier 2 also stay; v1 stops adding to them and deletes nothing (section 9). 0.x keeps each item's
  key on the paper (`papers.zotero_key`, `zotero_attachment_key`, left in place by `db adopt`); the `zotero`
  integration seeds its own state table from them on first start, so it updates the existing items and creates no
  duplicates.
- **Notion and n8n.** The current workflows keep working against 0.14 until cutover, then are
  replaced by the webhook workflow.
- **Cutover.** v1 runs beside 0.14 on a copy of the database for at least a week. The switch
  happens when v1's Watch output contains everything 0.14 put at tier 1–2 on the CoT selections
  that week, or each difference has been looked at. The owner's feedback given before cutover (labels, reactions,
  appraisals, saves and unsaves) carries over: the side-by-side database is the one it was given in, or it is
  copied into it.

## 8. Milestones

Each ends with something that runs and a stated check. Sizes are relative, not dates. Nothing runs on a schedule
before the side-by-side week of M7: jobs are run by hand, and a test overnight run, if one is wanted, gets temporary
infrastructure of its own.

**M0 · Skeleton.** New package layout, migrations, `litwatch.toml`, `core` types, `store`
repositories for papers, `intake` ported, CI. *Check:* a fresh database ingests a night of arXiv;
0.x intake tests pass against the new modules.

**M1 · Model gateway.** `llm` with both providers, roles, journal, retries, refusal handling,
price table. *Check:* the same request runs through either provider by changing one line of
configuration; journal cost matches hand calculation on recorded responses.

**M2 · Watch pipeline and its evaluation.** Topics with kinds, triage, keyword audit, grading
with two labs, reconciliation, `feedback.replay`.

Labels are seeded only from the entries of `evals/labeled.yaml` whose provenance is
`gold: Andrei`, about a dozen papers. The rest of that file is silver (model agreement, or a
hand pass still awaiting him) and joins a separate regression set, together with 0.14's CoT
verdicts. Neither is ground truth: they are model output, and they contain none of the papers
0.14 itself missed.

That leaves almost no ground truth, and labelling a few hundred papers up front is more than
the owner will do, so the milestone asks for little and lets the rest accumulate:

- The papers 0.14 put at tier 1 or 2 on the two CoT selections, 77 on 2026-10-08, are accepted
  in bulk as provisional positives. The owner broadly agrees with them and corrects any he
  notices; nobody reads them one by one.
- The owner skims 100 papers that triage rejected, drawn at random with a recorded seed, 70
  from rejects that triage placed in the interpretability or safety fields (the nearest
  neighbours of the CoT topics) and 30 from all other rejects. Most are an obvious no from
  the title. This is the only look at what the gate drops.
- Candidates the new pipeline surfaces are labelled on the labelling page as the owner finds time. Production
  precision is therefore not known at the end of this milestone; it is read from the first calibration samples
  (M3), and the model defaults are re-checked then.

A label may be yes, no or unsure. Unsure is excluded from every metric and kept as a list of
borderline cases. The owner has not yet pinned down exactly what counts as relevant to each
CoT topic, and that list is the material for sharpening the rubrics, which matters more than
any number computed on shaky labels.

The triage and grading defaults are trialled on these. *Check:* every regression-set paper
that 0.14 placed at tier 1 or 2 becomes a candidate, or each loss has been looked at (the
set's negatives carry no requirement; how many become candidates is reported); triage and
candidate recall on the provisional positives, and the number of Watch papers found in the
rejected sample, are written to `evals/`; the model defaults in section 4.4 are chosen
provisionally, to be confirmed against the calibration samples.

**M3 · API and web app, Watch first.** The design pass is done (`docs/v1/DESIGN.md`, 2026-10-11). The API with its
OpenAPI schema and generated client; the writer; Digest, Paper, Topics, Papers and Settings over Watch data, with the
review sections of the issues arriving in M4. Labels, misses and Calibrate answers flow into the evaluation; reactions
feed the correction rate.

**Calibrate**, on the Quality page, is where the owner gives their word on purpose, and it replaces the labelling page
built in M2. The reading side offers feedback and never asks; Calibrate asks, because the owner went there to answer.
A session shows one paper per screen (title, abstract, its takes) and is meant to take minutes. It draws, per Watch
topic, ten papers uniformly with a recorded seed from those graded `relevant` or above in the trailing 30 days and
without a label, plus ten from those graded below. On request it continues with the papers waiting for a word: last
night's splits, reported misses not yet marked, and papers carrying a word-carrying reaction. Each paper asks:

| Question | Answers | Required |
| --- | --- | --- |
| Fit, for each Watch topic | about it (then must read or worth reading), near it, not, unsure | for the topic it was drawn or taken for |
| Value to the owner, whatever the topic | matters, fine, not for me | no |
| Each take it has | accurate, off | no |
| Note | free text | no |

Fit is a label: about it is relevant at the chosen grade, near it is adjacent, not is not relevant. A drawn paper's
label carries origin `sample`, any other `reader`. Value and note are an appraisal, and accurate or off a take rating
(section 2). Precision at `relevant` is the share of the first drawn group labelled relevant, unsure excluded, and
the second group reports how often grading dropped a paper the owner wanted.

The writer's model is chosen once, outside the app: takes from Opus 5.5 and one or two OpenRouter models on about 20
recent papers, read blind by the owner.

*Check:* the daily issue reads as prose with no grade, model or pipeline term on it, the status line's
split count and the folded "how this was chosen" being the two allowed exceptions; earlier
issues are reachable by scrolling; a paper can be opened, saved and exported as BibTeX in one tap each; a topic's
papers can be sorted and filtered as a table; the status line reflects a failed run and a split; a reported miss shows
what each stage said, under Settings; with no integration connected every page works; nothing about the pipeline is
visible without opening Settings; Unsave takes a verdict-saved paper out of the library and a later re-grade does not
put it back; a Calibrate session of twenty papers takes minutes and gives the precision number, and a paper marked
as mattering with no topic fit appears on the "matters, no topic" list.

**M4 · Reviews and the weekly issue.** Radar and Pulse reviews at each topic's cadence, their sections in the daily
issue, the weekly issue with the Watch recap and the pinned row, the reviews on topic pages, in-process scheduler, Runs
and cost under Settings (including the papers triage is stuck on). *Check:* the weekly job, run by hand over two past
weeks, produces two weekly issues, and the owner prefers them to 0.14's digests or says what is missing; the scheduler
starts each job at its cron time, shown with a test schedule rather than overnight.

**M5 · Published versions.** `publications` with DBLP, monthly re-check, manual override,
BibTeX from the published record. *Check:* on 50 library papers known to be published, the
lookup finds the right record for at least 45 and never a wrong one.

**M6 · Integrations.** Outbox and dispatcher; Zotero and WebDAV, archive, deep, webhook; the n8n
workflow for Notion. *Check:* with every integration disabled the system is fully usable; with
each enabled, its 0.14 behaviour is reproduced for Watch papers, and Zotero creates no duplicate of an item 0.x made; a small new integration can be
written from `docs/integrations.md` without editing any other package.

**M7 · Migration and cutover.** Data migration, a week side by side, switch production.
*Check:* the cutover rule in section 7.

**M8 · Release.** Compose file, install and configuration docs, repository clean-up, MIT
licence, `v1.0.0`.

After v1: backfill of papers outside the collection for Watch topics; citation counts; other sources.

## 9. Decisions and open questions

Decided on 2026-10-08:

1. **Zotero items from broad selections** stay where they are. v1 stops adding to them and
   deletes nothing.
2. **Check model.** Muse is the preferred default, for the third lab. Milestone 2 measures it
   against Luna at medium effort before the default is fixed; its latency tail (section 5) is
   the thing to watch.
3. **Radar picks** are not saved automatically; the owner saves the ones that matter.
4. **Reporting a miss from a phone** is the usual case. The web app is an installable PWA and
   registers as a share target, so on the owner's Android phone an arXiv link shared from any
   app arrives in litwatch. This is part of M3. Added 2026-10-11: the share target is the route `/papers?lookup=<url>`,
   which the PWA keeps through the token prompt on first use; the lookup fetches the paper if the collection lacks
   it and shows its row with "Should have been found for a topic…", which asks for the Watch topic and files the
   miss. A paper the collection already holds shows the same row; nothing is reported until the topic is chosen.
5. **The PI's view.** The Notion export carries Watch topics only, until the owner has talked
   to her.

6. **Search across litwatch and arxiv-archive** is one box and one result list, with no
   switch. litwatch always searches titles and abstracts in its own database. An integration
   may also answer searches, and `archive` does, which adds the papers litwatch never ingested
   (the archive searches titles and abstracts too; full-text search arrives if the archive
   gains it). Results are merged by arXiv id. A paper only the archive holds is shown as
   outside the collection, with one action that fetches and stores it like any new paper. It
   becomes a reported miss only if the owner marks it as one for a Watch topic. Where a result comes from is an ordinary filter beside topic, saved and
   year, with no default restriction. Backfilling older archive papers through Watch triage
   and grading stays after v1; jobs already take explicit paper sets for that purpose.
   Added 2026-10-11: every query goes to the archive when it is connected, not only when litwatch's own search comes
   up short. Hits merge by arXiv id and fall into two bands: collection rows first, archive-only rows under them. A
   search sorts each band by published date, newest first (archive-only rows have no added date and show none); the
   table without a query sorts by added date, as a topic page does. A `SearchProvider` hit may carry an optional
   score; when every hit in a band carries one, a "best match" sort orders that band by it, and litwatch's own
   unscored hits keep the date order. The year filter applies to both bands by published date; topic and saved apply
   to the collection band only. Semantic search is the archive's job, since it holds every paper, and is not built
   into litwatch. BibTeX export over a filter or selection covers archive-only rows too: a hit carries arXiv id, title, authors
   and published date, which is what litwatch builds an arXiv entry from for its own unpublished papers; a published
   record is looked up only for papers in the collection.
7. **What a topic edit re-runs.** Nothing, by itself: edits apply forward, and retroactive
   runs are explicit and scoped (section 3). Re-triaging history on every edit would cost
   several dollars each time at this collection size.

Decided on 2026-10-11, in the M3 design pass (`docs/v1/design.html`):

8. **The web app is a reading surface, not a console.** No inbox, no label queue, no "needs you". Feedback is offered
   on every paper and asked for nowhere. The pipeline's terms (grades, judge and check, splits, model names, reasons)
   appear in exactly two places outside Settings: the status line's split count and the folded "how this was chosen"
   on a paper page.
9. **Issues, not day pages.** The Digest is a feed of daily and weekly issues; the weekly issue is pinned for its week;
   a day without content shows the last issue rather than an empty page.
10. **Look: Masthead.** Warm paper, a newspaper masthead with the status line under it, a colour per topic carried
    from the Digest's sections to the topic's chip, card, rail entry and page, serif for titles and takes. Tokens in
    `docs/v1/DESIGN.md`.
11. **Configuration stays read-only in the web app** in v1: `litwatch.toml` is the source of truth, so there is no
    config-write path in the API. This bounds what the shared token can change about the deployment, not what it can
    do with the data: it authorises every other write (section 4.1, web).
12. **Thumbs and labels are separate records.** A reaction never writes a label. On a Watch topic, two of its reasons
    are the owner's word on the paper (section 2), and the newer of that and the label decides; clearing the reaction
    takes back only its own word. A label keeps the origin it was created with, so the evaluation's samples are never
    disturbed by a change of mind. (Amended 2026-10-12; the first version had reactions write labels of their own.)
13. **Takes are bound to text and rubric.** A take is keyed by paper revision and topic version and rewritten when
    either changes under a new verdict, so a take never describes an abstract or a rubric that no longer exists.
14. **Which splits the status line counts.** A split where the two graders straddle the `relevant` line (one says
    `relevant` or above, the other `adjacent` or `none`): that disagreement decides whether the paper was shown at all.
    A split between `relevant` and `must_read` keeps its stored flag but is not counted on the line.
15. **An issue is a record.** Its entries stay as they were the day it was built. An entry whose paper the owner later
    set to `adjacent` or not relevant is kept and rendered struck through with "set aside", not removed.
16. **A paper in two Watch topics** appears in both of its sections of the issue and in both topic tables, each entry
    with that topic's take and a line "also in <other topic>". The paper page carries one take per topic, each under
    its chip. The Papers table shows the take of the first such topic by the owner's order.
17. **Reactions on every paper.** Thumbs are on every paper wherever it is shown, with the topic of the surface it is
    shown on (a Watch topic, a Radar or Pulse field, or none for a plain search result). Only a Watch-topic reaction
    can be the owner's word (decision 12); the rest are collected and shown on Quality with no automatic effect. The
    owner corrects by hand from the collected feedback, or has an agent read it and adjust rubrics and settings.

Decided on 2026-10-12, reviewing the spec for the remaining milestones:

18. **Unsave.** Save and Unsave are one toggle on every paper; the owner's newest use of it beats every other reason
    to be in the library, including a later re-grade, until they save it again. Unsaving is not a label: the paper
    stays in its topics and past issues. Zotero hears `paper.unsaved` and tags the item as dropped.
19. **Announced once.** A paper appears in an issue at most once per Watch topic. If the owner sets it aside and then
    changes their mind, the old entry stops being struck through and no later issue repeats it.
20. **Quality is a calibration workspace.** The reading side and the Quality page have different jobs: the reading
    side offers feedback and never asks; Calibrate asks, because the owner opened it to answer, and may ask more than
    one question per paper. A paper is not one-dimensional: fit to each Watch topic, its value to the owner whatever
    the topic, and whether its take is accurate are separate answers. A paper that matters but fits no topic points
    at a missing topic or a narrow rubric. Calibrate replaces the M2 labelling page and is built in M3.
21. **Writer model.** Opus 5.5 on the Anthropic API, on credits, compared once in M3 with one or two OpenRouter models.
22. **No scheduled runs before cutover.** Jobs run by hand until the side-by-side week of M7; checks that mention a
    schedule are shown with a test schedule.

Nothing is open. Noted for later: the owner plans embeddings over the archive (abstracts,
ideally full text) for semantic search, once a GPU server exists. That would also give a
cheap way to pick candidates for backfill and for re-runs after a topic edit.
