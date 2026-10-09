# Claude Haiku 5.5 primary screening trial, October 8

Decision: keep `openai/gpt-5.6-luna` as the primary for now. At low effort Haiku 5.5 is
stricter than Luna around the tier 2 bar and loses papers production placed at tier 1 or 2.
Medium and high effort recover most of that at the primary stage, but have not been run
through the full cascade. No production settings or data changed.

## What was tested

Whether `claude-haiku-5-5`, served by Anthropic's API on credits, can replace Luna as the
primary without losing papers, and how much OpenRouter spend that removes.

119 papers, each through the full cascade twice: once with Luna low and once with Haiku 5.5
low as the primary. GLM 5.3 high re-screened and Muse Spark contributor high confirmed in
both arms, with production's provider lists. Same seven current selection specs, prompt v3,
strict schema and 8192-token cap. Groups:

- 12 gold and 8 older expected-tier-1 controls from `labeled.yaml`.
- 39 papers production placed at tier 1 (19 verdicts) or tier 2 this week, with production's
  final tiers as the expectation.
- 60 other papers from the same week, seeded sample.

A primary-only pass then ran Haiku 5.5 at medium and at high effort on the same 119.

## Results

All 238 cascades completed. Selection-level verdicts: 833 per arm.

| Metric | Luna low | Haiku 5.5 low |
| --- | ---: | ---: |
| Production tier 1–2 verdicts kept at tier 1–2, after primary | 49/50 | 37/50 |
| Same, after the full cascade | 46/50 | 37/50 |
| Production tier 1 verdicts kept at tier 1, after primary | 13/19 | 2/19 |
| Same, after the full cascade | 13/19 | 12/19 |
| Gold positive labels at tier 1–2, full cascade | 7/10 | 7/10 |
| Older controls at tier 1, full cascade | 6/8 | 7/8 |
| Papers sent to re-screen | 24/119 | 58/119 |
| Confirm calls on the 60 other papers | 18 | 5 |
| Primary cost, 119 papers | $0.0359 | $0.0385 |
| Rest of the cascade (GLM and Muse) | $0.1075 | $0.1429 |

Primary tiers disagreed on 143 of 833 verdicts: Haiku was stricter on 117 and more generous
on 26. The bulk is one step down: 21 of Luna's 38 tier 1 verdicts became tier 2, and 43 of
its 88 tier 2 verdicts became tier 3. A tier 3 never reaches confirmation, which is where
the 9-verdict loss after the full cascade comes from. Confirmation restored most tier 1s.

Haiku's prompt was about 4,900 tokens against Luna's 2,870 for the same text, and about
4,500 of them were read from the cache.

Primary only, Haiku 5.5 by effort, against the same production verdicts:

| Effort | Tier 1–2 kept | Tier 1 kept | Stricter / more generous than Luna | Low-confidence verdicts | Cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| low | 37/50 | 2/19 | 117 / 26 | 40 | $0.0385 |
| medium | 44/50 | 5/19 | 109 / 22 | 66 | $0.0533 |
| high | 47/50 | 6/19 | 97 / 26 | 85 | $0.0703 |

## Higher effort through the full cascade

The Haiku arm was rerun at medium and at high effort on the same 119 papers, against the
Luna arm above. All cascades completed.

| Primary | Tier 1–2 kept, final | Tier 1 kept, final | Papers re-screened | Primary cost | GLM and Muse cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| Luna low | 46/50 | 13/19 | 24/119 | $0.0359 | $0.1075 |
| Haiku 5.5 low | 37/50 | 12/19 | 58/119 | $0.0385 | $0.1428 |
| Haiku 5.5 medium | 41/50 | 11/19 | 77/119 | $0.0532 | $0.1817 |
| Haiku 5.5 high | 44/50 | 10/19 | 86/119 | $0.0712 | $0.2295 |

High effort brings tier 1–2 retention within Luna's rerun noise, and it costs more
OpenRouter money than Luna does, not less: 86 of 119 papers went to GLM. The cause is how the
two models use the confidence field. Luna gave no low-confidence verdict in 833, so its
re-screens are all keyword audits; Haiku gave 40 at low effort and more with each step up,
and every one triggers a re-screen. A Haiku primary would need the re-screen trigger
recalibrated for it before it could reduce spend.

## Reading it

- The production-positive group favours Luna: those papers are positive because the Luna
  cascade said so. Luna's own rerun kept 46 of 50, so 4 losses is the noise floor and Haiku
  low lost 13.
- Tier 1 is unstable in both arms: Luna's rerun kept 13 of 19 of production's tier 1s.
- One run per arm, no repeats. The tier 2 bar was softened on October 5 and the labels
  predate it, so "stricter" is a difference from production, not a measured error.
- Moving the primary to Haiku low removed the Luna charge and added about the same in GLM
  re-screens, because Haiku marks more verdicts low confidence. Part of its lower confirm
  spend is the lost positives. Higher effort raises low-confidence verdicts further.

## Spend

Completed trial $0.3247 as reported or computed, of which $0.0385 was Anthropic credits.
Two aborted starts cost $0.0701: the script's $0.15-per-call allowance exceeded the cap at
eight papers in flight, then a 90-second Muse timeout stopped the run. The effort passes cost
$0.1236 in credits. The two higher-effort cascades cost $0.5356, of which $0.1244 credits.
The trial used a modified `scripts/eval_primary.py` that takes `--reserve`, `--timeout`,
`--keep-going`, `--feed-sample`, `--candidate-effort` and `--candidate-only`, and reads a
`positives` group from the snapshot. That version is not on this branch: it lives in commit
`7911f79` on the unmerged `feat/anthropic-backend` branch, together with the Anthropic client
it needs. The command below runs only from that commit.

Inputs and outputs: `/srv/appdata/litwatch/evals/20261008-haiku-primary/` (snapshot sha256
`2951e062…7ea39`), outside the repository because it holds selection text.

```bash
uv run python scripts/eval_primary.py SNAPSHOT trial.jsonl --candidate claude-haiku-5-5 \
  --feed-sample 60 --concurrency 6 --reserve 0.02 --budget 0.9 --timeout 240 --keep-going
```
