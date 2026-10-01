# Provider routing for every OpenRouter request, October 1

Decision: every request now names its providers per model (`order` plus `only`), with
fallbacks allowed only within that list and `require_parameters` on. GLM 5.3 rescreen routes Baidu, Reka, then Wafer.
Models, efforts and prompts are unchanged. Kimi and Muse lists were chosen from endpoint
facts, not a trial.

## Production evidence (journal, September 16 to October 1)

18,456 calls across 13 runs cost $11.94: confirm $3.76, screen $2.96, rescreen $2.83,
digest $2.40. Rescreen had been routed freely across about 30 GLM endpoints. Together
and Morph served 22% of rescreens and reported zero reasoning tokens despite `high`
effort. Wafer, OpenRouter's default, served 19% of calls and 31% of rescreen spend.
Kimi digests landed on eight providers, several fp4, with no cache hits.

## Trial

40 production rescreen cases from runs 37 and 38 (41 selection verdicts), current
selection specs, high effort, 8192-token cap, one provider per request with no fallback.
Every request returned a response; DeepInfra's one failure was a truncated output at the
cap. Reported spend: $0.2346, plus that truncated response's uncounted charge (at most
about $0.02 at DeepInfra's output price; the first script version missed rejected
responses).

Agreement is leave-one-out: each provider against the most common tier among the other
providers, Together kept out of every reference.

| provider | agrees with the others | median reasoning tokens | cost/call | p50 / p90 s |
|---|---:|---:|---:|---:|
| Wafer | 39/41 | 222 | $0.00170 | 3.0 / 15.6 |
| Baidu (fp8) | 39/41 | 25 | $0.00016 | 1.4 / 3.6 |
| Reka | 39/41 | 29 | $0.00023 | 2.8 / 9.2 |
| Sail Research | 37/41 | 28 | $0.00062 | 1.3 / 2.7 |
| Fireworks | 37/41 | 29 | $0.00167 | 3.3 / 7.1 |
| DeepInfra (fp4) | 37/40 | 27 | $0.00062 | 3.4 / 50.7 |
| Together | 31/40 (one tie) | 0 | $0.00089 | 0.5 / 1.3 |

The six reasoning endpoints were unanimous on 33 of 41 verdicts; their differences are
two to four verdicts, which this sample cannot separate from noise. Together is clearly
worse. The reference is consistency, not ground truth, and the sample is mostly easy
cases. Wafer reasons longest and stays last as a fallback; if hard cases matter more
than cost, `LITWATCH_PROVIDERS="z-ai/glm-5.3=wafer"` restores it.

Expected effect: rescreen cost per call drops from about $0.0014 to about $0.0002, and
no rescreen is served without reasoning. A live smoke of all four stages through the new
routing was served by OpenAI Flex, Baidu, Meta and Wafer.

Reproduce: `scripts/eval_providers.py CASES.json OUT.jsonl --providers a,b,...
--not-reference together`; `--report-only` recomputes the table from OUT.jsonl. The
case file stays outside the repository because it contains selection text.
