# 2026-09-10: DeepSeek V4.1 Flash primary screening trial

Decision: retain Luna. DeepSeek did not pass the initial availability check;
quality and full-cascade cost were not established. No production configuration,
database, Zotero, or Notion data was changed.

## Intended comparison

Replace only `openai/gpt-5.6-luna` with `deepseek/deepseek-v4.1-flash`, both
at low effort, using the existing strict structured-output screening request.
The frozen September 9 snapshot contains 677 papers and six selection specs.
The quick trial selected 52 distinct papers: all gold-labeled and expected-tier-1
controls, plus 32 seeded random feed papers (seed 20260910). The spending stop
was $2, with four concurrent jobs, automatic retries disabled, and an 8192-token
completion cap. This cap is an evaluation safeguard, not the uncapped production
primary setting.

Success would require comparable or better recall on the controls and reviewed
disagreements, plus lower full-cascade cost. Most existing labels are silver;
even the gold labels include conversions marked unverified against the current
rubric. Historical Luna decisions are not ground truth.

## Observed result

- Two Luna primary requests succeeded in 6.90 and 7.45 seconds. Their combined
  provider-reported charge was $0.00177425.
- One DeepSeek primary request returned HTTP 429, `engine_overloaded`, from the
  upstream shared pool. The response named DeepInfra and an earlier Venice failure.
- A separate retry of the first paper returned the same 429 in 0.80 seconds.
- The other concurrent DeepSeek request remained pending for more than 90 seconds
  and was cancelled when ending the trial. Its charge is unknown. The reported
  Luna charge is therefore not a reconciled total evaluation bill.
- The accounting stop prevented downstream calls after the first error. No complete
  cascade, DeepSeek verdict, quality comparison, or cost saving was obtained.

OpenRouter's endpoint inventory at the time advertised `structured_outputs` on
DeepInfra and Venice, but not on the native DeepSeek or Novita endpoints. The
native endpoint's off-peak $0.15/M input and $0.60/M output pricing consequently
does not establish the price of Litwatch's existing strict-schema requests.
DeepInfra listed $0.30/M input and $1.20/M output; Venice listed $0.375/M and
$1.50/M. The live inventory and the actual errors should be rechecked before retrying.

## Reproduction and limitations

Evidence: `/srv/appdata/litwatch/evals/20260910-deepseek-primary/`, including
`paired.manifest.json`, `paired.calls.jsonl`, `paired.jsonl`, `retry.json`, and
`endpoints.json`. The manifest preserves the exact selected inputs.

`scripts/eval_primary.py` is the reusable bounded paired helper. After the initial
attempt, its per-call limit was hardened with a 90-second wall-clock timeout, and
its confirmation model became an explicit option defaulting to the live
`meta/muse-spark-1.3-contributor`. The initial attempt had specified regular Muse;
no confirmation requests occurred, so this did not affect the observations.
The helper refuses to resume evidence containing errors. Use a fresh output path
after reconciling unknown charges; keep the original failure evidence intact.

```sh
.venv/bin/python scripts/eval_primary.py \
  /srv/appdata/litwatch/evals/20260909-glm-high/litwatch-frozen.json \
  /srv/appdata/litwatch/evals/NEW-TRIAL/paired.jsonl \
  --confirm-model meta/muse-spark-1.3-contributor --budget 2
```

Validation: helper compiled successfully; no deployment was attempted. A future
JSON-mode or direct-provider adapter would be a different integration and must
be evaluated explicitly rather than described as this drop-in replacement.
