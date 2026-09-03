# Screener eval results

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
