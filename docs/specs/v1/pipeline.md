# What happens to a paper

Nightly, in order, then continuously for the full-text queue. Each step is a job that can also run alone over any set
of papers or date range, which is what re-runs and a later backfill use. Every job that spends money on more than one
night's papers shows its paper count and estimated cost first (see [models.md](models.md), budgets).

```
intake ─▶ classification ─▶ analysis ─▶ grading ─▶ rarity
                                            │
                                            └─▶ full-text queue (Brief, Weight re-check, Deep card)
                                                              │
                                    digests (end of run, after a bounded wait) ◀─┘
```

## 1. Intake

Unchanged from the first v1: OAI-PMH for the configured categories, arXiv pacing, revision handling (section 8).
Every paper intake stores is registered with the archive, which fetches its LaTeX source and, by the storage
policy, its PDF (see [archive.md](archive.md)). Registration is delivered through the outbox like an integration's
work (durable, retried with backoff, idempotent on the archive's side), so an archive outage delays registration and
never loses it; intake's checkpoint does not wait for it. Intake emits `paper.added` and `paper.revised`.

A second intake source reads the papers of a date window and the configured categories from the archive instead of
arXiv, and stores them as the nightly source does (they are already registered there). The restart, the sandbox and
later the backfill use it.

Every paper intake stores is processed by classification and analysis, so the collection is exactly what intake
stored plus papers the owner added (a miss, a paper processed on demand from the archive).

## 2. Classification

One call per paper to the `classify` role, a decision model: Jev, chosen in milestone N1 over Luna Decisions and
Haiku 5.5 (`evals/2026-10-14-classifiers.md`). The request carries the paper's title and abstract and the taxonomy:

- one yes/no question per class, "does this paper belong to <class name>: <description>", answered with a
  probability;
- one choice question for the primary class among all classes.

A paper is a **direct member** of a class when its probability is at or above the class's threshold: its own if set,
otherwise 0.5 for a class at or under a Focus topic and 0.8 for every other class (`[classification]` in configuration;
set in N1, where 0.8 kept recall at 0.96 and raised precision from 0.43 to 0.72 on the silver memberships),
and of its primary class, which is the choice answer, whatever that class's own yes/no probability (the choice is the
stronger statement). It is an **inherited member** of every ancestor of a class it is a direct member of. Only direct
memberships are stored; inherited ones follow from the tree. A paper can be a direct member of a class and of its
ancestor at once (both passed their thresholds); that is two direct memberships.

A response is **valid** only if it answers every question, every probability is in [0, 1], and the choice names a
class of the current tree. An invalid response is a failed call: retried like any failed call, and after three
failed runs the paper is awaiting classification (section 9).

The owner's class label outranks the classifier for that class: member adds a direct membership (source `owner`),
not removes the paper from the class, whatever its probability. Inheritance and grading topics follow from what
remains.

Decision models choose by the option's name more than by the definition attached to it ("Type-Safe Is Not
Error-Free", arXiv 2609.26758), so class names must say what they mean on their own; [taxonomy.md](taxonomy.md)
holds to that.

**Recall insurance for Focus topics.** Two more ways into a Focus topic's class, both stored as the source of the
membership:

- **uncertain band**: a probability in [0.1, threshold) for a class at or under a Focus topic makes the paper a
  member for grading only; it is graded, and only grading can show it;
- **keyword check**: the Focus topic's keyword patterns are matched against title and abstract with no model
  involved; a match makes the paper a member for grading only.

A membership records the class version and paper revision it was made from. The probabilities of all classes are kept,
so a threshold change can be replayed without new calls.

## 3. Analysis

One call per paper to the `analyse` role, a small LLM (Haiku 5.5 class), from title and abstract:

- the **Gist**, 20–30 words, about the paper and not about any class, in litwatch's objective voice (no "for you",
  no reader-directed wording);
- **Weight** (landmark, solid, incremental; defined in section 4) with one sentence why, judged from the abstract
  against the paper's own area, its primary class from classification, and recorded with source `abstract`;
- **facets**: contribution kind (method, analysis, benchmark or dataset, theory, position, survey), and whether the
  abstract announces released code and released weights.

Facets are facts, not scores, and are not combined into anything. No other dimension (writing quality, rigour,
novelty) is scored: each would need its own calibration against the owner's word, and an abstract cannot show most of
them. Rigour and limitations are the Deep card's job.

Analysis is bound to the paper revision. A rewrite (section 8) analyses the paper again. A paper that gets a written
Brief has its Gist written again from the full text (section 6).

## 4. Grading

**Which topics grade a paper.** For each of the paper's direct memberships, take the nearest followed class at or above
that class; the paper is graded once for each distinct topic found this way. Inherited memberships never add a topic.
Examples, with Interpretability followed as an Area and CoT faithfulness as a Focus topic under it:

- direct member of Sparse autoencoders only: graded for Interpretability;
- direct member of CoT faithfulness only: graded for CoT faithfulness, not for Interpretability;
- direct member of CoT faithfulness and of Sparse autoencoders: graded for both;
- direct member of CoT faithfulness and of Interpretability itself: graded for both.

Memberships from the uncertain band and the keyword check are direct memberships of the Focus topic's class and
count for that topic only. A paper with no direct membership under any followed class is not graded.

**What a parent topic shows.** A parent topic's page, digests and pick candidates include every paper in its subtree.
A paper the parent graded itself shows the parent's grade; otherwise the highest grade among the followed
descendants that graded it; otherwise it is ungraded there (its Weight gives its colour, section 5).

Grading judges **Fit**: central, related, adjacent or none, with one sentence why, for that topic, from title,
abstract, the topic's subject and, for a Focus topic, its rubric. How it is checked depends on the topic's depth
(the assurance level, [models.md](models.md)):

| Depth | Graders | Disagreement |
| --- | --- | --- |
| Focus | `judge` and `check`, from different labs, concurrently | the more inclusive Fit stands; the verdict is marked split and shown on Quality |
| Area | `judge`, then the `area_check` decision model asked whether the Fit holds | on a "no", or a "yes" below `[grading] area_check_min_confidence` (default 0.7), `check` (the other lab) grades too; if the two Fits differ, the less inclusive stands and the verdict is marked split, since an Area's risk is a false pick, not a miss |
| Field | `field_grade`, one cheap model | none |

A verdict needs every grader it calls to answer validly (a Fit from the four, a reason; for `area_check`, a yes or no
with its probability). If one fails, comes back truncated or invalid, or refuses, no verdict is made:
the grading stays pending, is retried next run, and after three failed runs is listed on Quality as ungraded with a
button to grade it now (carried from the first v1). As in classification and analysis, only an unusable answer (truncated, invalid,
refused) counts toward the three; a grader that did not answer (a timeout, a provider down) is retried without
counting. Grading it now runs at once, as a run of its own, and counts as the first of three more runs; until its result is
recorded the grading stays pending, so a request cut short leaves it to the next run.

Grading runs nightly after analysis, once per paper revision: the first pass on a revision grades every active topic
its memberships lead to (a paused topic grades nothing new; a paper whose analysis failed is graded all the same, its
Weight treated as solid). After that pass only failed gradings and reported misses are graded; a topic the paper
leads to later, by a followed topic or a moved class, waits for a re-run (section 10).

### Fit and Weight

Fit and Weight are two axes. **Fit** is how close a paper is to one topic; **Weight** is how much the paper matters,
whatever topic it is graded for. Each level has one definition, given word for word to every model that judges it
(the graders and `area_check` for Fit; `analyse` and `brief` for Weight):

| Fit | Meaning |
| --- | --- |
| central | The paper's main question is the topic's subject. |
| related | A substantial part of the paper is about the subject, or its main result is something work on the subject would use directly, but the subject is not its main question. |
| adjacent | The paper touches the subject (uses its methods as a tool, asks a neighbouring question, cites it as motivation) without adding to it. |
| none | The paper is in the topic's subtree by classification but is not about the subject. |

A topic's subject is its Fit definition: what is in it and, where a neighbour is close, what is out. A Focus topic's
rubric spells out what central, related and adjacent mean for that topic (anything below adjacent is none), and where it places a kind of paper at a level, the
rubric wins over the general definitions above: a Focus topic is a narrow research question, and its rubric says how
much of that question a paper must answer to be central (a single-task audit can be on the subject and still only
related). Area and Field topics carry no rubric.

| Weight | Meaning |
| --- | --- |
| landmark | Its area will treat it as a reference: a new model family or training recipe with released weights, a large margin on a widely used benchmark, a method shown to work across several domains, or strong evidence overturning a common assumption. Rare: about one or two papers in a hundred in an area. |
| solid | A complete, sound contribution others in its area would build on: a method with a fair comparison, a careful analysis, a useful benchmark. |
| incremental | A small step: a minor variant, a single narrow application or evaluation, a survey or position paper without new evidence. |

Weight is judged against the paper's own area, its primary class: what is a landmark in one area can be incremental
in another. A paper is given one Weight, not one per topic (decision 53). A paper with little Fit to a topic but a
landmark Weight is not hidden: its grade there is adjacent, and in a mixed list its Weight gives it at least rare
(section 5).

### Grade

The grade for a topic is derived from Fit there and the paper's Weight:

| Fit \ Weight | landmark | solid | incremental |
| --- | --- | --- | --- |
| central | must read | must read | relevant |
| related | must read | relevant | adjacent |
| adjacent | adjacent | adjacent | adjacent |
| none | none | none | none |

This table was checked in N2c against the two-week run and 0.14's tiers and kept for now: the labels available did
not establish a cell change ([evals/2026-10-15-tuning.md](../../../evals/2026-10-15-tuning.md)). A paper whose
analysis has not produced a Weight yet is treated as solid. A Focus topic's digest lists papers by Fit, not grade (see [digests.md](digests.md)).

**Effective Fit and effective grade.** The owner's word is always a Fit (see [feedback.md](feedback.md)). The
**effective Fit** is the owner's word where there is one, otherwise the verdict's. The **effective grade** is the
table applied to the effective Fit and the current Weight, with one floor: an owner's word of central or related never
yields less than relevant. Every surface, export and integration uses the effective Fit and grade.

## 5. Rarity

Rarity is what a paper's colour shows. Five levels, low to high: common, uncommon, rare, epic, legendary.

**On a topic page** (one topic, its grade alone): none → common, adjacent → uncommon, relevant → rare, must read →
epic, must read with Weight landmark → legendary.

**In a mixed list** (Digest, Papers, search): the highest of the paper's levels, where each followed topic's level is
its topic-page level lowered by the topic's depth (Focus 0, Area 0, Field 2), and a paper's Weight alone gives a
floor (landmark → rare, solid → uncommon, incremental → common), which is all an ungraded paper has. A paper outside
the collection has no rarity and shows none.

Like the grade table, the depth offsets are tuned in N2. Rarity is recomputed whenever a grade, Weight or the owner's
word changes, and when the tree or the followed topics change, and stored, so lists sort and filter by it without
recomputing.

The badges a paper shows in a mixed list are its followed topics, the one giving the highest level first.

## 6. Full-text queue

A **full-text pass** reads the paper's text, not its abstract. Three kinds:

| Pass | For which papers | Does |
| --- | --- | --- |
| Brief | Focus papers at effective Fit central or related | writes the Brief and the Gist; also re-checks Weight |
| Weight re-check | Area papers whose grade from the abstract is must read (the pick candidates) | judges Weight again from the full text, and writes a Brief and the Gist |
| Deep card | Focus papers at an effective grade of must read; Area picks; any paper on request | asks the `deep` integration for a card (see [integrations.md](integrations.md)) |

A pass is queued when its paper qualifies, judged on the effective Fit and grade, so the owner's word can queue one
too (a label or miss that makes a Focus paper central gets it a Brief). Only active topics queue one; a pass not yet started is dropped
when its paper no longer qualifies (the owner's word, a paused or unfollowed topic). The database decides, not the events that
queue: the worker settles a pass's paper before it spends anything on it, so a pass no longer wanted is dropped
unread, and the nightly run settles every paper with a pass open or that may qualify for one, so a missed trigger
costs at most a night. It is **awaiting text** until the archive has the paper's text, then runs at
once, not at the next night: the service's full-text worker, which runs with the schedule, looks at the queue every
half minute (`litwatch fulltext` works it by hand). Text comes from the archive: the LaTeX source's text first, the PDF's text when the paper
has no source (see [archive.md](archive.md)). For a pass queued by the nightly run, litwatch asks the archive to fetch
the source at **urgent** priority, so the papers that matter arrive within minutes rather than behind a night's
background downloads.

**The Brief** is one paragraph (about 120–200 words): what was done, the main result with its numbers, and the main
limit. When the full text does not support the abstract's claim, the Brief opens with one line saying so, for example
"The abstract overstates: the gain holds only for 7B models on GSM8K." A paper without a Brief shows its abstract in
the Brief's place (a Field paper, an Area paper that is not a pick candidate, an adjacent Focus paper, or a pass
still awaiting text).

**The Gist from the full text.** The call that writes a Brief also returns Weight and a new Gist, so the text is read
once and the Gist cannot repeat a claim the Brief corrects. The Gist keeps the rules of section 3 and has its own
instructions in the response schema. It replaces the Gist from the abstract as Weight does: the current Gist is the
one from the full-text pass queued last for the current revision, else the one from analysis; a new one emits `gist.written`.

**Weight from the full text** replaces Weight from the abstract (source `full_text`) and the grade, rarity and
library follow. A digest already written does not change (it is a record, [digests.md](digests.md)).

**What a pass read.** A pass is queued for one paper revision and records what it read: the arXiv version of the
text, its source (LaTeX or PDF) and a hash of it. When the archive cannot say which arXiv version its text is, the
version is recorded as unknown. A pass that finishes after its paper got a new revision stores its result labelled
with the old revision (section 8), does not replace the current Weight, and a pass for the new revision is queued. A
running pass holds a lease (default 30 minutes); when the lease runs out, because the worker died, the pass goes back
to ready.

A pass that fails, including a model response that is truncated or invalid, is retried on the next run (a download
the archive gave up on is asked for again with it); after three
failed runs it is listed on Quality with the error and a button to run it now. A pass awaiting text for more than
seven days is listed there too. Both count as terminal for the digest wait ([digests.md](digests.md), timing).

## 7. The library

An effective grade of relevant or above on a Focus topic saves a paper. Area picks and any other paper are saved by
hand. The owner's newest Save or Unsave outranks every other reason (carried from the first v1, decisions 18). The
library is what integrations act on. Details in [feedback.md](feedback.md).

## 8. Revisions

Carried from the first v1 (0.14's rule). A new arXiv version whose title or abstract is rewritten (similarity below
0.8) raises the paper's own revision number; classification, analysis and grading run again on it, and every record
says which revision it was made from. A revision whose earlier text held a grade above none is graded again even when
classification no longer puts it in that topic's subtree, so a grade on text that no longer exists cannot stay
current. A Brief or Deep card made from an older revision is kept and labelled with it until a new one exists. A
Gist from an older revision's full text is not: the new revision's analysis gives the current Gist.

A new arXiv version that leaves title and abstract close enough does not raise the revision, but its body may have
changed: a paper that has a Brief or a Weight from the full text gets those passes queued again for the new version.
A Deep card is labelled with the version it read and is remade only on request.

## 9. Failures that need the owner

Classification is the gate for everything after it, so a paper whose classification fails three runs in a row is
**awaiting classification**, counted on the status line and listed on Quality, with three actions carried from the
first v1's awaiting-triage: **dismiss** (litwatch drops it from processing; it stays searchable in the archive),
**classify by hand** (the owner names its classes; it goes on as usual with source `owner`), **fix the text** (the
owner corrects title or abstract; this counts as a rewrite and classification runs again). A paper whose analysis
fails three runs in a row is counted on the status line, listed on Quality and no longer tried, so it costs nothing until the owner acts: **try
again** gives it three more runs. It has no Gist or Weight until then and shows its title alone.

## 10. Edits to the taxonomy and to topics

Edits apply forward. A class's version is the hash of its name, description, parent and threshold; a topic's of its
depth, subject, rubric, interests and keywords. Records refer to the revision they were made under. After an edit the
app offers scoped re-runs, each with paper count and estimated cost: the owner's labels and reported misses (does the
edit help), the topic's existing members, papers matching the topic's keywords, a date range. Nothing retroactive runs
by itself (first v1, decision 7).

Moving a class in the tree, splitting or merging classes, or following a different class changes at once how existing
memberships resolve (below), but runs no classification or grading: papers are graded under the new shape only when
classified or graded after the change, or by a re-run.

**How older records read under the current tree.** Memberships are stored per class; inheritance, grading topics,
topic pages and digest selection always use the current tree and the current followed topics. A verdict belongs to
its topic and stays valid there whatever version of the tree it was made under. A paper whose memberships now lead to
a topic that has no verdict for it shows ungraded there (its Weight colours it) until a re-run grades it. A retired
class (deleted, or replaced by a split or merge) keeps its memberships in the data, but they lead nowhere; papers
reach the new classes only through a re-run. Rarity is recomputed from the current tree and the existing verdicts.
Published digests are records and are never re-projected.

## 11. Volumes and cost

From the side-by-side database (2026-09-29 to 2026-10-12): 700–1,550 papers on a weekday, about 2,500 on the first day
after a weekend, none announced on Saturday and Sunday. Those are nightly intake records, 31 % of them a version after
the first, in a fortnight that began in the busiest week of the first v1's data. Measured in N2c on the papers first
submitted 2026-10-01 to 10-14 (6,305, read from the archive): 425–595 on a submission weekday (424 on the partial last
day), 975 and 1,156 on a Monday with Saturday and Sunday folded in
([evals/2026-10-15-tuning.md](../../../evals/2026-10-15-tuning.md)). Estimates per weekday at about 1,000 papers, with
what N2c measured per 1,000 papers:

| Step | Calls | Estimate | Measured (N2c) |
| --- | --- | --- | --- |
| Classification | 1 decision request per paper (~400 input tokens, ~50 questions) | about $0.05 a day on Luna Decisions | $0.26 on Jev |
| Analysis | 1 small-LLM call per paper | $0.50–1 a day | $0.40 |
| Grading, Focus | 20–40 members a night, two labs | cents, as in the first v1 | $1.00 for 145 gradings, 132 of them from the uncertain band and the keyword check |
| Grading, Area | 30–60 members a day, `judge` plus a decision check | a few dollars a day on Anthropic credit | $0.87 for 179 gradings, 2 of them escalated to `check` |
| Grading, Field | a few hundred members a day, one cheap model | under $1 a day | $0.16 for 811 gradings |
| Full-text passes | a handful of Briefs and re-checks a day | cents to a dollar | not run; the grades queue about 62 papers (56 Area re-checks), about $9 at N3's $0.15 a pass |
| Deep cards | Focus must reads, Area picks, requests: a few a week | the `deep` service's own subscription | not run; Focus must reads alone are about 4 |
| Digests | one call per digest written | as the first v1's reviews | not run |

Classification, analysis and grading came to $2.68 per 1,000 papers, two thirds of it the `judge` on Opus 5.5. The
run processed the fortnight at once; its costs split by submission day, weekends folded into Monday, give $1.04–1.70
for a night Tuesday to Friday and $2.45–3.29 for a Monday.

The restart (milestone N6) runs this over the two most recent weeks of papers, read from the archive.
