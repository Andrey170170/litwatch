# A batched night (model-spend plan, P3)

Run on 2026-10-16, branch `feat/nightly-batches` (on `feat/brief-sonnet`: `brief` on Sonnet 5.5 at high), on the
scratch database `spend_night`: migrations through 0027, the seed taxonomy, and the 541 papers the live archive
listed for 2026-10-14 (its listing read only, as N6 did). The night was given rather than read from the schedule
([night.py](2026-10-16-batched-night/night.py)): start 18:59 UTC, deadline 22:59, `--limit 300` a step, digests of
2026-10-15. **$3.35 in all**, by the call journal: Anthropic $3.15, OpenRouter $0.19.

## Batches

The first run was killed (SIGKILL) once its Brief batch was recorded; the second ran with the same window.

| Run | Batch | Calls | Sent | Answers read | Minutes |
| --- | --- | --- | --- | --- | --- |
| 4 | analyse | 300 | 19:00 | 19:09 | 9 |
| 4 | field_grade + judge | 418 | 19:09 | 19:18 | 9 |
| 4 | brief | 5 | 19:19 | 19:42, by run 5 | – |
| 5 | analyse | 241 | 19:20 | 19:28 | 8 |
| 5 | field_grade + judge | 279 | 19:28 | 19:42 | 14 |
| 5 | brief | 22 | 19:42 | 19:49 | 7 |
| 5 | brief, texts that arrived later | 17 | 19:49 | 19:56 | 7 |
| 5 | writer, the five topics' digests together | 5 | 19:56 | 20:08 | 12 |

- **Each step is one batch** (R4): the step's calls are queued at once, and only passes whose text came later made a
  second Brief batch. The night took 69 minutes, the kill included; from 02:00 it ends long before 07:30.
- **Every Anthropic call went in a batch**: 1,287 calls, none sent normally, none failed, none left unsettled.
  OpenRouter's roles (`classify`, `check`, `area_check`) went as normal calls, as the plan has it.
- **Batch prices are half.** A Brief costs $0.051 a pass (44 passes, $2.26), against $0.094 for the same model as
  normal calls in P1 and Opus 5.5's $0.208 in N6.

## A run killed and started again

Run 4 died with 5 full-text passes leased (3 Briefs, 2 Area Weight re-checks) and their 5 calls in an open batch. Run
5 took back the 5 leases (`fulltext_taken_back: 5`), asked their calls again and attached to run 4's batch: no call
was submitted twice (one submission for each of the night's 1,287 calls), and the 5 answers landed on run 4's journal
rows, at batch price. The passes finished.

## Cost

| Role | Calls | Cost |
| --- | --- | --- |
| brief | 44 | $2.261 |
| judge | 196 | $0.622 |
| writer | 5 | $0.116 |
| analyse | 541 | $0.107 |
| field_grade | 501 | $0.048 |
| classify (OpenRouter) | 541 | $0.138 |
| check (OpenRouter) | 94 | $0.051 |
| area_check (OpenRouter) | 103 | $0.004 |

$6.19 per 1,000 papers, against N6's $17.06. At about 12,700 papers a month that is about $71 a month on Anthropic
before digests (about $4 a month more for a month's dailies and weeklies at this night's $0.023 a digest) and about
$4.50 on OpenRouter. The plan's $60 assumed N6's share of papers with a full-text pass (about 6.5 %); this night's 541
papers had 8.1 %. At N6's share it is about $65. Either way it is under the $100 cap.

## Not shown here

- **The journal against Anthropic's own usage, by the hour** (V9, R6). The console shows usage only by day. For
  2026-10-16 the owner read $65: Opus $51, Sonnet $11, Haiku the rest. The journals of that day's runs come to $62.14:
  - Opus $48.44: N6's restart $47.70 and this night $0.74.
  - Sonnet $11.90: P1's replay $9.64 at normal prices and this night $2.26.
  - Haiku $1.80.

  The $3 gap is calls these journals do not hold, such as production 0.14's on that day. This night's Sonnet calls charged at normal
  prices would have made Sonnet's day about $14, not $11, so the batch tier did apply. Its exact charge cannot be read
  apart from the day.
- **The deadline** is shown by tests with a fake clock (V7, `tests/test_batches.py`), not by this night, which ended
  three hours before it.
