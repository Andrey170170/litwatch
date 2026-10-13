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

Every paper intake stores is processed by classification and analysis, so the collection is exactly what intake
stored plus papers the owner added (a miss, a paper processed on demand from the archive).

## 2. Classification

One call per paper to the `classify` role, a decision model (the default is chosen in milestone N1 from Luna
Decisions, Jev and Haiku 5.5). The request carries the paper's title and abstract and the taxonomy:

- one yes/no question per class, "does this paper belong to <class name>: <description>", answered with a
  probability;
- one choice question for the primary class among all classes.

A paper is a **direct member** of a class when its probability is at or above the class's threshold (default 0.5),
and of its primary class, which is the choice answer, whatever that class's own yes/no probability (the choice is the
stronger statement). It is an **inherited member** of every ancestor of a class it is a direct member of. Only direct
memberships are stored; inherited ones follow from the tree. A paper can be a direct member of a class and of its
ancestor at once (both passed their thresholds); that is two direct memberships.

A response is **valid** only if it answers every question, every probability is in [0, 1], and the choice names a
class of the current tree. An invalid response is a failed call: retried like any failed call, and after three
failed runs the paper is awaiting classification (section 9).

Decision models choose by the option's name more than by the definition attached to it ("Type-Safe Is Not
Error-Free", arXiv 2609.26758), so class names must say what they mean on their own; [taxonomy.md](taxonomy.md)
holds to that.

**Recall insurance for Focus topics.** Two more ways into a Focus topic's class, both stored as the source of the
membership:

- **uncertain band**: a probability in [0.2, threshold) for a class at or under a Focus topic makes the paper a
  member for grading only; it is graded, and only grading can show it;
- **keyword check**: the Focus topic's keyword patterns are matched against title and abstract with no model
  involved; a match makes the paper a member for grading only.

A membership records the class version and paper revision it was made from. The probabilities of all classes are kept,
so a threshold change can be replayed without new calls.

## 3. Analysis

One call per paper to the `analyse` role, a small LLM (Haiku 5.5 class), from title and abstract:

- the **Gist**, 20–30 words, about the paper and not about any class, in litwatch's objective voice (no "for you",
  no reader-directed wording);
- **Weight** (landmark, solid, incremental) with one sentence why, judged from the abstract, recorded with source
  `abstract`;
- **facets**: contribution kind (method, analysis, benchmark or dataset, theory, position, survey), and whether the
  abstract announces released code and released weights.

Facets are facts, not scores, and are not combined into anything. No other dimension (writing quality, rigour,
novelty) is scored: each would need its own calibration against the owner's word, and an abstract cannot show most of
them. Rigour and limitations are the Deep card's job.

Analysis is bound to the paper revision. A rewrite (section 8) analyses the paper again.

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
| Area | `judge`, then the `area_check` decision model asked whether the Fit holds | on a "no", or a "yes" below `[grading] area_check_min_confidence` (default 0.7), `check` (the other lab) grades too, and the Focus rule applies |
| Field | `field_grade`, one cheap model | none |

A verdict needs every grader it calls to answer validly (a Fit from the four, a reason; for `area_check`, a yes or no
with its probability). If one fails, comes back truncated or invalid, or refuses, no verdict is made:
the grading stays pending, is retried next run, and after three failed runs is listed on Quality as ungraded with a
button to grade it now (carried from the first v1).

### Grade

The grade for a topic is derived from Fit there and the paper's Weight:

| Fit \ Weight | landmark | solid | incremental |
| --- | --- | --- | --- |
| central | must read | must read | relevant |
| related | must read | relevant | adjacent |
| adjacent | adjacent | adjacent | adjacent |
| none | none | none | none |

This table is a starting point, tuned in N2 against the owner's labels. A paper whose analysis has not produced a
Weight yet is treated as solid. A Focus topic's digest lists papers by Fit, not grade (see [digests.md](digests.md)).

**Effective Fit and effective grade.** The owner's word is always a Fit (see [feedback.md](feedback.md)). The
**effective Fit** is the owner's word where there is one, otherwise the verdict's. The **effective grade** is the
table applied to the effective Fit and the current Weight, with one floor: an owner's word of central or related never
yields less than relevant. Every surface, export and integration uses the effective Fit and grade.

## 5. Rarity

Rarity is what a paper's colour shows. Five levels, low to high: common, uncommon, rare, epic, legendary.

**On a topic page** (one topic, its grade alone): none → common, adjacent → uncommon, relevant → rare, must read →
epic, must read with Weight landmark → legendary.

**In a mixed list** (Digest, Papers, search): the highest of the paper's levels, where each followed topic's level is
its topic-page level lowered by the topic's depth (Focus 0, Area 0, Field 1), and a paper's Weight alone gives a
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
| Brief | Focus papers at Fit central or related | writes the Brief; also re-checks Weight |
| Weight re-check | Area papers whose grade from the abstract is must read (the pick candidates) | judges Weight again from the full text, and writes a Brief |
| Deep card | Focus papers at must read; Area picks; any paper on request | asks the `deep` integration for a card (see [integrations.md](integrations.md)) |

A pass is queued when its paper qualifies. It is **awaiting text** until the archive has the paper's text, then runs at
once, not at the next night. Text comes from the archive: the LaTeX source's text first, the PDF's text when the paper
has no source (see [archive.md](archive.md)). For a pass queued by the nightly run, litwatch asks the archive to fetch
the source at **urgent** priority, so the papers that matter arrive within minutes rather than behind a night's
background downloads.

**The Brief** is one paragraph (about 120–200 words): what was done, the main result with its numbers, and the main
limit. When the full text does not support the abstract's claim, the Brief opens with one line saying so, for example
"The abstract overstates: the gain holds only for 7B models on GSM8K." A paper without a Brief shows its abstract in
the Brief's place (a Radar or Field paper, an adjacent Focus paper, or a pass still awaiting text).

**Weight from the full text** replaces Weight from the abstract (source `full_text`) and the grade, rarity and
library follow. A digest already written does not change (it is a record, [digests.md](digests.md)).

**What a pass read.** A pass is queued for one paper revision and records what it read: the arXiv version of the
text, its source (LaTeX or PDF) and a hash of it. When the archive cannot say which arXiv version its text is, the
version is recorded as unknown. A pass that finishes after its paper got a new revision stores its result labelled
with the old revision (section 8), does not replace the current Weight, and a pass for the new revision is queued. A
running pass holds a lease (default 30 minutes); when the lease runs out, because the worker died, the pass goes back
to ready.

A pass that fails, including a model response that is truncated or invalid, is retried on the next run; after three
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
current. A Brief or Deep card made from an older revision is kept and labelled with it until a new one exists.

## 9. Failures that need the owner

Classification is the gate for everything after it, so a paper whose classification fails three runs in a row is
**awaiting classification**, counted on the status line and listed on Quality, with three actions carried from the
first v1's awaiting-triage: **dismiss** (litwatch drops it from processing; it stays searchable in the archive),
**classify by hand** (the owner names its classes; it goes on as usual with source `owner`), **fix the text** (the
owner corrects title or abstract; this counts as a rewrite and classification runs again). A paper whose analysis
fails is retried and, after three runs, listed on Quality; it has no Gist or Weight until then and shows its title
alone.

## 10. Edits to the taxonomy and to topics

Edits apply forward. A class's version is the hash of its name, description, parent and threshold; a topic's of its
depth, subject, rubric, interests and keywords. Records refer to the revision they were made under. After an edit the
app offers scoped re-runs, each with paper count and estimated cost: the owner's labels and reported misses (does the
edit help), the topic's existing members, papers matching the topic's keywords, a date range. Nothing retroactive runs
by itself (first v1, decision 7).

Moving a class in the tree, splitting or merging classes, or following a different class changes which topic grades a
paper only for papers classified or graded after the change, unless a re-run is asked for.

**How older records read under the current tree.** Memberships are stored per class; inheritance, grading topics,
topic pages and digest selection always use the current tree and the current followed topics. A verdict belongs to
its topic and stays valid there whatever version of the tree it was made under. A paper whose memberships now lead to
a topic that has no verdict for it shows ungraded there (its Weight colours it) until a re-run grades it. A retired
class (deleted, or replaced by a split or merge) keeps its memberships in the data, but they lead nowhere; papers
reach the new classes only through a re-run. Rarity is recomputed from the current tree and the existing verdicts.
Published digests are records and are never re-projected.

## 11. Volumes and cost

From the side-by-side database (2026-09-29 to 2026-10-12): 700–1,550 papers on a weekday, about 2,500 on the first day
after a weekend, none announced on Saturday and Sunday. Estimates per weekday at about 1,000 papers, to be measured in
N1 and N2:

| Step | Calls | Estimate |
| --- | --- | --- |
| Classification | 1 decision request per paper (~400 input tokens, ~50 questions) | about $0.05 a day on Luna Decisions |
| Analysis | 1 small-LLM call per paper | $0.50–1 a day |
| Grading, Focus | 20–40 members a night, two labs | cents, as in the first v1 |
| Grading, Area | 30–60 members a day, `judge` plus a decision check | a few dollars a day on Anthropic credit |
| Grading, Field | a few hundred members a day, one cheap model | under $1 a day |
| Full-text passes | a handful of Briefs and re-checks a day | cents to a dollar |
| Deep cards | Focus must reads, Area picks, requests: a few a week | the `deep` service's own subscription |
| Digests | one call per digest written | as the first v1's reviews |

The restart (milestone N6) runs this over one week of papers plus the evaluation papers.
