# DeepSeek V4.1 Flash retry: September 11

Decision: retain Luna. Strict structured output now succeeds on some requests,
but primary-call timeouts prevented completion of the bounded comparison.
No production settings, database records, Zotero items, or Notion pages changed.

The live configuration was verified before the trial: Luna low primary,
GLM 5.3 recall, and Muse Spark 1.3 contributor confirmation. Reused the frozen
September 9 feed and the same 52-case sampling procedure as the September 10
trials. Both models used low primary effort, an 8192-token completion ceiling,
and the existing strict-schema request. The ceiling is a trial safeguard;
production primary calls are uncapped. Automatic retries were disabled.

## Availability and latency

- A standalone V4.1 canary succeeded in 33.14 seconds, costing $0.0010248.
- The paired run admitted eight papers (16 cascade jobs) at concurrency eight:
  four simultaneous V4.1 branches and four Luna branches.
- All eight Luna cascades completed. Five of eight V4.1 primary requests
  succeeded; three hit the 90-second wall-clock limit (`2507.12379`,
  `2507.22928`, `2512.01222`). Their Luna primary calls took 4.83, 5.85,
  and 7.93 seconds, respectively.
- A fourth V4.1 branch (`2510.04040`) completed its primary call in 87.26
  seconds and one confirmation, then the accounting stop prevented another
  downstream call. This is not a fourth provider timeout.
- Four complete pairs remained for comparison. No random-feed cases were run.

The public endpoint inventory now lists Fireworks, Modal, Wafer, Morph,
DeepInfra, and Venice as advertising structured outputs. The experiment used
default OpenRouter routing; it did not pin a provider or establish which provider
served each timed-out request. Endpoint availability alone does not demonstrate
reliable service for this workload. The observed timeouts do not establish that
launch load, rather than inference latency or another issue, was their cause.

## Limited paired result

| Four complete control pairs | Luna | V4.1 Flash |
| --- | ---: | ---: |
| Primary cost | $0.00315175 | $0.00918873 |
| Full cascade cost | $0.00832341 | $0.01431137 |
| Labeled positives retained in tiers 1/2, primary and final | 3/4 | 3/4 |
| Final exact-tier matches | 3/4 | 3/4 |

V4.1 cost about 72% more across these completed cascades. Four controls are too
few to establish quality parity or ordinary daily cost, and excluding failed
requests introduces survivorship bias. This result does not meet the user's
"as good or better and cheaper" replacement condition. The cost comparison
excludes the canary, incomplete pairs, and unresolved timeout charges.

## Evidence and accounting

Directory: `/srv/appdata/litwatch/evals/20260911-deepseek-primary/`.
`canary.json`, `endpoints.json`, `paired.manifest.json`, `paired.calls.jsonl`,
`paired.jsonl`, and `analysis.json` preserve inputs, completed usage, verdicts,
errors, and the provider inventory.

Total reported cost, including the canary and completed calls on incomplete
pairs: **$0.037337834**. Three timed-out requests have unresolved charges; this
is not a reconciled final bill. The paired helper used a $1.90 budget after the
canary, with $0.15 reserved per in-flight call, stopping on the first error.
No further batches or automatic retries were submitted.

```sh
.venv/bin/python scripts/eval_primary.py \
  /srv/appdata/litwatch/evals/20260909-glm-high/litwatch-frozen.json \
  /srv/appdata/litwatch/evals/NEW-TRIAL/paired.jsonl \
  --candidate deepseek/deepseek-v4.1-flash --budget 1.9 --concurrency 8
```

Luna remained the verified live primary after the trial. No source/runtime
changes were needed; only this experiment log and its index were added.
