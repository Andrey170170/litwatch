# Screener eval results

## 2026-09-09 · GLM high, targeted recall

[Full frozen-feed evaluation](2026-09-09-glm-high.md): 140 targeted checks versus
233 full-scope Sol calls; recall cost $0.44 versus $0.91. Comparable daily projection
$1.30 at 677 new papers, including today's digest cost. Tier-1 assignments unchanged;
two tier-2 additions and one loss. Evaluation cost $0.55.

## 2026-09-03 · prompt v2 · Luna low, Sol low re-screen

Labeled set: 166 papers at run time. 14 gold (Andrei's verdicts), 152 silver (the
Luna/Sol draft and an independent Terra-high judge agree on tier; Terra's
flags break flag-level ties). 9 contested papers, where draft and Terra
disagree on tier, are in `contested.yaml` awaiting a human label.

| tier | P first | R first | P final | R final | n |
|---|---|---|---|---|---|
| must_read | 0.93 | 0.96 | 0.93 | 0.98 | 53 |
| aware | 1.00 | 0.92 | 1.00 | 0.92 | 24 |
| mech_interp | 0.94 | 0.94 | 0.97 | 0.94 | 34 |
| none | 1.00 | 1.00 | 1.00 | 1.00 | 55 |

Flag accuracy: cot 0.98, internals 0.99, mech_interp 0.97, foundational 0.95.
Cost: $0.20 for the run ($0.05 Luna, $0.15 Sol on 46 re-screens).

Caveat: silver labels were produced by the same prompt family, so agreement
on them is expected and the numbers above are an upper bound. The honest
signal is the gold subset: one must_read miss out of 12, "Among Us"
(2504.04072), deception detection with probes and SAEs that never mentions
the chain of thought. Three further gold must_reads are papers Terra labels
mech_interp: mechanistic analyses of how CoT is computed (2402.18312) and
internals-based hallucination detection (2510.11529, 2601.03052). Whether
those belong in tier 1 is a topic-definition question, not a model error.

Update, same day: the three gold must_reads Terra labels mech_interp, plus
"Among Us", were moved to `contested.yaml`. Andrei rates his first-pass
labels as low confidence, and whether that wider scope belongs in tier 1
is a question for the PI. The labeled set is now 162 papers; the tier 1
scope stays at the PI's wording until she comments on concrete cases.

## 2026-09-03 · backfill, real feed · prompt v2 · Luna low, Sol low re-screen

14-day window over cs.CL, cs.AI, cs.LG: 3,695 papers, 270 re-screened (7%),
$2.28, 15 minutes. Tiers: 35 must_read, 10 aware, 241 mech_interp, 3,409 none.

Terra at high effort re-judged the 45 tier 1 and 2 papers ($0.18). It agreed
on 20 of 35 must_reads and 5 of 10 awares. 23 of the 35 must_reads reached
tier 1 through the foundational flag alone, and that is where most of the
disagreement sits: "faithful" and "trace" in unrelated senses (formal math
proofs, trading strategies, disaster assessment) read as faithfulness
benchmarks. Internals-based must_reads were mostly confirmed. Precision on
the real feed is therefore well below the eval-set number; the eval set was
built from topical searches and had few of these near-negatives. The 25
agreed papers joined `labeled.yaml` as silver, the 20 disagreements joined
`contested.yaml`. Recall on the real feed is unmeasured.

## 2026-09-03 · confirm stage on the backfill positives

Judges compared on the 45 tier 1 and 2 papers (same prompt, high effort):
Terra kept 20 of 35 must_reads, GLM 5.3 kept 24, DeepSeek V4 Pro 15, Muse
Spark 1.3 kept 13. Pairwise agreement between judges was 27 to 33 of 45, so
none of this is ground truth. Muse's four lone rejections read as correct
applications of the definition (reasoning-correctness verifiers, a
text-to-image plan paper), so Muse became the confirm stage.

Applied to the live database with `litwatch reconfirm`: 45 confirmed, 26
changed tier. Routed set after all three stages: 10 must_read, 9 aware, out
of 3,695 papers. Cost of the confirm pass: about $0.60 on the regular Muse
endpoint.

## 2026-09-07 · selections v3 · Luna low, Sol low re-screen, Muse high confirm per selection

The tier scheme changed: verdicts are per selection on a 1..4 scale, and the
labeled set carries an expected tier per selection (schema 2). The
`cot-faithfulness` rubric is the PI's, from BonaFide's definition; see
`src/litwatch/seeds/`. Run: `evals/runs/20260907T185457Z.json`, 191 papers,
$0.34, seeds at versions 62badefff02c / ac05422d497d / d9aef060102b.

How the labels were made. The v1 flags were converted mechanically
(must_read -> faithfulness 2, aware -> 3, mech_interp flag -> mech-interp 2),
which is why the raw table below reads badly: tier 1 precision 0.09 and
tier 1+2 recall 0.38 against those labels. The disagreement list was then
reviewed. Every paper the cascade put in tier 1 was read against its
confirmed reason: 9 of 11 are tier 1 under the rubric (model-editing and
hint ground truth, probes against the trace, meta-evaluations of
hint-acknowledgment classifiers), 2 are tier 2. Of the 37 old positives the
cascade demoted to tier 3 or 4, all but one were monitoring, steganography,
encoded reasoning, grounding, or correctness papers, exactly what the rubric
excludes; their labels now follow the verdict and are marked silver. The
one real miss was Selective Disclosure of Hidden Directives (2608.29070,
gold tier 1 -> 3, "does not establish whether the trace faithfully reports
computations"); the rubric gained a sentence saying that disclosure of a
demonstrably followed hint, cue, or directive is an omission test. Why2Speak
(2608.20670) went from a hand-pass tier 1 to tier 2 on the rubric's own
single-task exclusion, which the confirm model applied correctly.

Raw table on the converted labels, before the review (final stage):

| selection | tier | P | R | n |
|---|---|---|---|---|
| cot-faithfulness | 1 | 0.09 | 0.33 | 3 |
| cot-faithfulness | 2 | 0.60 | 0.21 | 57 |
| cot-faithfulness | 3 | 0.31 | 0.63 | 35 |
| mech-interp | 2 | 1.00 | 0.60 | 63 |

On the 39 papers labeled by hand (28 from abstracts on 2026-09-07 plus
BonaFide and Lanham, and 11 tier 1 candidates reviewed against their
confirmed reasons, which flatters tier 1):

| tier | P | R | n |
|---|---|---|---|
| 1 | 0.82 | 0.90 | 10 |
| 2 | 0.88 | 0.58 | 12 |
| 3 | 0.47 | 0.80 | 10 |
| 4 | 0.67 | 0.29 | 7 |

Tier 1+2 recall on that subset is 0.82. The tier 2/3 boundary is where the
model is inconsistent, as expected for "recommended" versus "good to know";
the tier 1 boundary is the one the rubric defines and it holds. Labeled set
after the review: 193 papers, 12 tier 1, 24 tier 2, 64 tier 3 for
`cot-faithfulness`; 52 papers carry a `cot-monitoring` label, all silver.
Caveat as before: silver labels come from the same prompt family, so the
next run's numbers on them are an upper bound; the hand-labeled subset is
the signal to watch, and the PI's own labels on the boundary cases will
replace it.

Second run, same day, after the review and the rubric sentence
(`evals/runs/20260907T190436Z.json`, 193 papers, $0.33, cot-faithfulness at
0dc850a0bcbd): tier 1 P 0.78 R 0.58, tier 2 P 0.78 R 0.75, tier 3 P 0.84
R 0.88, tier 1+2 recall 0.89. Selective Disclosure now lands in tier 1. Luna
alone has tier 1 recall 0.75; the confirm stage demotes five expected tier 1
papers to tier 2 by applying the ground-truth clause literally: FaithCoT-Bench
(expert-annotated labels, no hint or internals ground truth), Hypocrisy Gap
(one benchmark only), Post-Hoc Reasoning and Reasoning Theater (probes show a
pre-committed answer but the reasons argue no explicit omission test), and
Lanham et al. (intervention metrics, which the rubric itself files under
importance). All five stay visible as tier 2. Whether human-annotated
benchmarks and probe-versus-trace analyses should count as tier 1 is a
rubric question for the PI; the sentence to loosen is the one starting "The
knowledge of what the model actually computed must come from".
