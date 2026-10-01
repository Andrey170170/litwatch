# GPT-6 Luna primary screening trial, September 22

Decision: keep `openai/gpt-5.6-luna` as the primary. GPT-6 Luna is cheaper and
faster at low effort, but missed too many frozen positive controls. Medium effort
did not restore tier-1 control retention. No production settings or data changed.

## Method

The frozen September 9 snapshot supplied six selection specs and 52 papers:
12 gold cases, eight additional expected-tier-1 controls, and 32 seeded random
feed papers. Both models received the same title, abstract, categories, selection
order, prompt, strict output schema, 8192-token completion cap, and OpenRouter
Flex-then-standard routing. All 104 low-effort primary calls succeeded and were
served by `openai/flex`. A separate medium-effort pass ran GPT-6 Luna on the 20
labeled cases, also with 20 successful Flex responses. The primary trial did not
write verdicts to the production database.

Existing labels are regression controls, not uniformly independent human ground
truth. Some gold cases predate rubric changes; the random feed has no labels.
The 8192-token cap is a trial safeguard; production primary screening is uncapped.

## Primary results

| Metric | GPT-5.6 Luna low | GPT-6 Luna low | GPT-6 Luna medium |
| --- | ---: | ---: | ---: |
| Additional tier-1 controls retained at tier 1 | 8/8 | 4/8 | 4/8 |
| Additional controls retained at tier 1 or 2 | 8/8 | 7/8 | 5/8 |
| Gold positive labels retained at tier 1 or 2 | 7/10 | 4/10 | 7/10 |
| Gold tier-1 labels retained at tier 1 | 3/4 | 1/4 | 2/4 |
| Primary cost, all 52 cases | $0.015734 | $0.006587 | — |
| Median primary latency, all 52 cases | 14.39 s | 4.09 s | — |

Medium-effort GPT-6 cost $0.006140 across its 20 labeled calls, versus $0.006950
for low-effort GPT-5.6 on the same 20. Its median labeled-call latency was
about 15.6 seconds, close to GPT-5.6's low-effort latency. The low-effort
GPT-6 pass was about 58% cheaper for primary calls across all 52 papers. These
are observed charges and times, not a daily-cost or full-cascade projection.

On the 32 unlabeled feed papers, the low-effort primaries disagreed on 22 of
192 selection tiers. The new model sometimes downgraded tier-1 or tier-2
verdicts, and sometimes upgraded them; those differences require human review
before treating them as accuracy evidence. In the labeled controls, the tier-1
retention regression is sufficient reason to avoid a production cutover now.

## Full-cascade and availability checks

The existing paired full-cascade evaluator completed three gold and seven feed
pairs. On the seven feed pairs, reported total cascade cost was $0.009778 for
GPT-5.6 and $0.006290 for GPT-6. This is too small a sample to project a daily
bill or confirmation quality. A Muse confirmation timed out in the fourth gold
pair after GPT-6's primary response succeeded. A later feed branch stopped on
an OpenRouter connection error. The evaluator stopped at each error; neither
incomplete branch was scored as a completed pair. These failures do not establish
that GPT-6 itself is unavailable.

Known provider-reported test charges across the two interrupted cascade runs,
the complete low-effort primary trial, and the medium-effort control pass total
**$0.062314**. A timed-out or disconnected request may have an additional
unknown charge, so this is not a reconciled provider invoice. The request
limits and known charges remained well within the approved $2 cap.

## Follow-up

Keep GPT-5.6 Luna as the production default. `LITWATCH_SCREEN_MODEL` can opt
into GPT-6 Luna for a future isolated trial; both Luna IDs use the verified
`openai/flex`, `openai` routing order. Any later cutover should first explain
the control misses, then compare complete-cascade cost and final tier decisions
on a larger paired run. The frozen snapshot and detailed trial output remain
outside the repository because they contain selection text.
