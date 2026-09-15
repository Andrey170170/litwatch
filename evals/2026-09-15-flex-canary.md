# Luna Flex compatibility and accounting canary — September 15, 2026

Three explicitly approved live requests used the existing Litwatch OpenRouter
credential, public abstracts, and the seven current production selection specs.
Only primary screening ran; production papers, verdicts and configuration were not
modified. The call journal and reuse check used disposable PostgreSQL.

Settings: `openai/gpt-5.6-luna`, low effort, provider order
`openai/flex,openai`, provider fallbacks enabled, 120-second request deadline,
8192 completion-token safety cap. The cap is specific to this canary; production
primary screening retains its existing uncapped output setting.

| Paper | Seconds | Input | Cached input | Cache writes | Output | Reported cost |
|---|---:|---:|---:|---:|---:|---:|
| 2204.14226 | 37.36 | 2,424 | 0 | 2,421 | 368 | $0.000523725 |
| 2206.10397 | 12.69 | 2,479 | 2,166 | 310 | 345 | $0.000267710 |
| 2207.12631 | 12.81 | 2,483 | 2,166 | 314 | 337 | $0.000263410 |

All responses reported provider `OpenAI`, service tier `flex`, and validated against
the same strict response schema used for screening. All seven selection verdicts
were present. Total reported cost was **$0.001054845**. Cached input represented
**58.65%** across the three requests, including the cold first call, and about
**87%** on each subsequent call. No explicit prompt cache controls were added.

Replaying the first exact request returned its persisted validated output with
`reused=true`, `cost=0`, its original cost, and a reference to the original call.
The journal total did not increase. No fourth provider request was made.

The charges match the published Flex rates, including cache-write pricing:
$0.10/M ordinary input, $0.01/M cached input, $0.125/M cache writes and
$0.60/M output. These are half the corresponding standard Luna rates.
Sources: [OpenRouter Luna endpoints](https://openrouter.ai/api/v1/models/openai/gpt-5.6-luna/endpoints)
and [service-tier routing](https://openrouter.ai/docs/guides/features/service-tiers).

## Limits

This is a compatibility, billing and persistence canary, not a quality evaluation
or a daily latency forecast. It contains only three sequential requests and did
not exercise recall, confirmation, high concurrency or provider-capacity fallback.
The observed tier discount does not guarantee a 50% reduction in daily primary
costs: standard fallbacks, output lengths and cache behavior affect the result.

Offline regression coverage exercises invalid-output billing, failure of a later
confirmation followed by reuse in a fresh run, request/spec invalidation, explicit
rescreen invalidation, timeouts, and digest retry accounting using real PostgreSQL.
