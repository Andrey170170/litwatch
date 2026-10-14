# Evaluation labels

The labelled papers v1's classifier and graders are measured against (decision 55). They are files, never rows in a
live database. Each file is one or more **cohorts**; reports give numbers per cohort and never pool them. Paper text
is not stored: an evaluation reads titles and abstracts when it runs. Topic and class columns hold slugs from
[`src/litwatch/seeds/taxonomy.yaml`](../../src/litwatch/seeds/taxonomy.yaml).

| File | Rows | Cohorts | What it says |
| --- | --- | --- | --- |
| `fit.csv` | 302 | `owner`, `provisional` | Fit on the two CoT Focus topics |
| `regression.csv` | 1,153 | `legacy`, `silver` (its `source`) | the first v1's regression set for the CoT topics |
| `tiers-0.14.csv` | 23,310 | 0.14 | 0.14's selection tier per paper, one column per matching class |
| `silver-sample.csv` | 200 | silver | the papers drawn for the class sample, with their stratum |
| `silver-opus.csv`, `silver-sol.csv` | 198, 200 | silver | each labeller's primary class, other classes and reason |
| `silver.csv` | 165 | silver | the papers both labellers gave the same primary class |
| `disputed.csv` | 35 | silver | the papers they did not, with whether the owner was asked |
| `adjudicated.csv` | 20 | adjudicated | the owner's primary class for the papers asked |

## Where they came from

Exported on 2026-10-14, before the first v1's deployment stopped, from its database and from the development
database (an adopted copy of 0.14's production database, which the first v1 had added its labels to). The
development database held every label and regression row of the deployment, plus more; the regression set is the
union of the two (the deployment had 23 rows the development copy did not). No row disagreed between them.

**`fit.csv`.** The first v1's labels on its two Watch topics, mapped to Fit: relevant → central, adjacent → adjacent,
not relevant → none. `legacy` keeps the first v1's value and grade (`relevant/must_read`, `relevant/relevant`,
`adjacent`, `not_relevant`). `owner` rows are the owner's: `gold` came from 0.x label files, `reject_sample` from the
owner reading a sample of the papers triage rejected. `provisional` rows are 0.14's tier 1–2 papers on the two CoT
selections, carried as positives the owner has not confirmed (`origin` `legacy_positive`).

**`regression.csv`.** The first v1's regression set: `legacy` rows are 0.14's tiers (1 must read, 2 relevant,
3 adjacent) on the CoT selections; `silver` rows were graded by the first v1's two labs. It is kept as it was.

**`tiers-0.14.csv`.** Every paper 0.14 screened (August to October 2026), with its tier on each selection, empty when
0.14 did not screen it for that selection. The `mech-interp` selection was Interpretability's earlier name and is
folded into `interpretability`, keeping the better tier. Read as silver memberships of the class of the same slug:
tier 1–2 is a member, tier 4 is not, tier 3 is left out.

**The silver class sample.** `silver.py` draws it, labels it and compares the labellers; its docstring has the
commands. 200 papers from `tiers-0.14.csv`, drawn with seed 20261014: 20 positives of the CoT selections, 10 papers
the owner labelled, 20 positives of each of the five broad selections, and 70 papers no selection placed above
tier 4. Two labs sorted each paper into the seed tree separately, from its title and abstract (read from the
development database): Opus 5.5 (Anthropic) and GPT-6.1-Sol (OpenAI, through OpenRouter), both at medium effort, for
$1.32 in all. Opus refused 2 papers, which stay unresolved. Where both named the same primary class it is the silver
label, with the union of their other classes. Where they differed, 20 papers were drawn with the same seed,
round-robin over the top-level class Opus named, and the owner chose; the rest are `unresolved`, counted and left out
of primary-class accuracy.

Agreement on the primary class was 165 of 198 papers both answered (83 %); 22 of the 35 disputed papers differ only
below the same top-level class. The sample has no agreed paper in AI and society or Other: 0.14's screened papers
come from cs.CL, cs.LG and cs.AI and rarely land there.
