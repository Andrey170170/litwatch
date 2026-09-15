# GLM 5.3 Flash versus Luna: paired screening trial, September 10

Decision: keep Luna. Flash worked with the existing strict structured-output
request and matched Luna on first-pass must-read controls, but did not reduce
the cost of the unchanged cascade. No production settings or data were changed.

## Method

Candidate `z-ai/glm-5.3-flash` versus `openai/gpt-5.6-luna`, both low effort.
Both used the same frozen six selection specs, title/abstract/category inputs,
prompt, 8192-token completion ceiling, and OpenRouter request shape. The primary
ceiling is an evaluation safeguard; production primary screening is uncapped.
Downstream stages were GLM 5.3 high with the existing targeted recall rules and
Muse Spark 1.3 contributor high with per-selection confirmation. The broad
`benchmark` keyword remained excluded from recall escalation.

The preselected sample had 52 papers: 12 gold-labeled cases, eight additional
expected-tier-1 controls, and 32 random papers from the September 9 frozen feed
of 677 papers, sampled with seed 20260910. Existing gold labels include older
rubric conversions marked unverified. Other controls are not independent human
ground truth. Obsolete label slugs absent from the six frozen specs were not
scored or silently mapped to new selections.

The first run completed 19 paired cases; a downstream call on the final control
(`2307.13702`, Flash branch) hit the 90-second wall-clock timeout. Its Flash
primary call had already succeeded. The incomplete pair is excluded from paired
quality and cost totals. The 32 untouched random-feed cases then ran separately
and all completed, yielding **51 complete pairs** overall. Control concurrency
was four; feed concurrency was eight, balanced between both branches. No automatic
retries or production writes occurred.

## Cost: separate ordinary feed from deliberately difficult controls

Provider-reported dollars, complete paired cases only:

| Cohort / stage | Luna | GLM Flash |
| --- | ---: | ---: |
| Random feed, 32 primary calls | 0.01702450 | 0.01374033 |
| Random feed, recall | 0.00252483 | 0.01597031 |
| Random feed, confirmation | 0.01182751 | 0.00669365 |
| **Random feed, full cascade** | **0.03137684** | **0.03640429** |
| Gold subset, 11 full cascades | 0.02932611 | 0.05514566 |
| Additional controls, 8 full cascades | 0.01830482 | 0.03542413 |

Flash's primary cost was 19% lower on the random sample; full-cascade cost was
16% higher. Flash triggered eight recall calls versus Luna's two, partly offset
by thirteen Muse confirmations versus twenty-one. The control cohort was about
90% more expensive with Flash. Mixing these cohorts would exaggerate a projected
daily cost difference, so no daily bill is extrapolated from this small sample.

Measured average full-cascade latency on random-feed cases was 15.0 seconds for
Flash versus 23.0 seconds for Luna. This includes different downstream call counts
and provider/cache variability, not isolated model inference speed. Small fixed
batches and sequential confirmations made overall experiment time much longer.

## Quality and alternative explanations

- Both primary models assigned tier 1 to all **11 expected-tier-1 cases in the
  complete pairs** (three gold cases plus eight additional controls).
- Both full cascades retained 14/17 labeled positives in tiers 1 or 2: six of
  nine in the gold subset and all eight additional controls. Because of label
  provenance and rubric drift, this is a regression comparison, not a reliable
  absolute recall estimate.
- Flash's final tier-1 retention was 10/11 versus Luna's 7/11. This does **not**
  demonstrate a Flash advantage: both primary models initially agreed on these
  cases, and Muse made different subsequent judgments. Among 45 confirmation
  requests with identical paper/spec inputs across branches, four differed in
  tier. Three explain the apparent must-read advantage.
- The random feed has no independent labels. It produced three tier-2 versus
  tier-3 per-selection disagreements, all in the direction of Luna recommending
  and Flash assigning only "good to know". These are not necessarily papers
  lost from every selection or from Zotero.

| Paper | Selection | Luna final | Flash final | Review observation |
| --- | --- | ---: | ---: | --- |
| 2609.08306, HoneyRoute | alignment-and-safety | 2 | 3 | Cross-domain malicious-request detection and jailbreak payload handling appear within the authored subject; Flash's serving-infrastructure exclusion seems too narrow. |
| 2609.08410, Compositional Multilingual and Behavioral Attribute Steering | alignment-and-safety | 2 | 3 | Jailbreak steering across four models overlaps the subject, though whether safety is central is debatable. |
| 2609.07247, Elastic Horizon | reasoning-and-rl | 2 | 3 | Agentic RL interaction-horizon control overlaps RL post-training; the reasoning-versus-agent boundary warrants a human judgment. |

These observations come from reading saved abstracts and rubrics, not new gold
labels. In all three cases the disputed Flash tier 3 had medium confidence, so
that selection did not trigger recall (which checks low confidence or eligible
keyword-backed tier 4). The observed mechanism is therefore the cascade's tier-3
gap, not evidence that full GLM reinforced a shared-family mistake.

Shared family is only a hypothesis about correlated errors. The experiment does
not establish model training provenance or error correlation, and those claims
are not needed for the keep-Luna decision. Token discounts, routing, caching,
downstream stochasticity, and the small sample also limit generalization.

## Evidence and spending

Directory: `/srv/appdata/litwatch/evals/20260910-glm-flash-primary/`.
`paired.*` records the initial control run; `feed.*` records the random sample;
`analysis.json` combines complete pairs and records the timeout. Manifests preserve
the exact cases and specs; call ledgers preserve tokens, costs, and durations;
`endpoints.json` preserves the public provider inventory.

Total **reported** charge was $0.208127, including completed calls on the
incomplete control. Its timed-out request has an unresolved charge, so this is
not a reconciled final bill. The feed run used a $1.50 spending stop, leaving
more than the helper's $0.15 per-call allowance for that single unresolved request
within the original $2 trial envelope. Actual feed charges were $0.06778113.

Helpers: `scripts/eval_primary.py` and `scripts/analyze_primary.py`. The evaluator
now accepts a candidate, group, and bounded concurrency; refuses resumption of
error records or unmatched call charges; and applies a per-call wall-clock limit.
Syntax compilation and Ruff checks passed. The live primary remained
`openai/gpt-5.6-luna` after the experiment.

```sh
.venv/bin/python scripts/eval_primary.py \
  /srv/appdata/litwatch/evals/20260909-glm-high/litwatch-frozen.json \
  /srv/appdata/litwatch/evals/NEW-TRIAL/paired.jsonl \
  --candidate z-ai/glm-5.3-flash --budget 2
```

Changing the confidence/escalation policy might make Flash competitive, but that
would be a separate controlled experiment. This trial tested a primary-model
replacement under the current policy and did not meet the cheaper condition.
