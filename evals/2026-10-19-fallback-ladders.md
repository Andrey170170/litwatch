# The fallback replay (failure-handling plan, P1)

Run on 2026-10-19, branch `eval/fallback-ladders`, as a dry run on the scratch database `fh_p1_sandbox` (copied
from production that day) and the host archive, read only. Nothing was written to verdicts, the outbox or any
evaluation table: each call's answer went to a JSON-lines file in
[2026-10-19-fallback-ladders/runs/](2026-10-19-fallback-ladders/runs/). Each role's request is the one its pipeline
step sends, built by the step's own prompt builder, and was sent to the role's model and to every candidate alone.
**$9.03 in all** (at least: the calls that failed have no known charge), over 1,548 calls, against an estimate of
$12.49 printed before the run ([estimate.txt](2026-10-19-fallback-ladders/runs/estimate.txt)) and a $25 ceiling
the run never reached.

The cases are [select.py](2026-10-19-fallback-ladders/select.py)'s ([cases.csv](2026-10-19-fallback-ladders/cases.csv)),
the candidates [ladders.toml](2026-10-19-fallback-ladders/ladders.toml)'s. The tables are `litwatch eval
ladders-report`'s ([tables.md](2026-10-19-fallback-ladders/tables.md)), over the calls to each case's request as
it is built now. Production's own answers to the `field_grade` and `analyse` cases are exported by
[baseline.py](2026-10-19-fallback-ladders/baseline.py) to [production.csv](2026-10-19-fallback-ladders/production.csv)
and compared there with the role's own model re-asked and with each candidate
([baseline.md](2026-10-19-fallback-ladders/baseline.md)), which also counts what each candidate answered and where it
differs from the role's own model.

The run's calls went out as the pipeline sends them, OpenRouter's uncapped. Since the run, the harness bounds a
call's cost: it sends each OpenRouter call capped at the ladder's `reserve_output_tokens` (above every output the
run's calls made), and reserves the input at a token a byte, at each candidate's dearest price (`price_max`, added
to [ladders.toml](2026-10-19-fallback-ladders/ladders.toml)). None of these is part of a call's identity, and no
recorded call cost more than its reservation as reckoned now (the last line of tables.md).

## The cases

**Quality set, 297 cases of ordinary papers** (299 drawn; the pair below is left out of `judge` and `check`).

- `judge` and `check`: the owner's labels. Production has no Calibrate session and no label, so they are the owner
  cohort of [evals/labels/fit.csv](labels/fit.csv), on two Focus topics (cot-faithfulness, cot-monitoring). That is
  every pair labelled central, related or adjacent (22) and 60 of the pairs labelled none, 82 pairs. One pair,
  2609.18985, is left out: the paper is in neither the database nor the archive. **81 pairs each.**
- `field_grade` and `analyse`: 60 of production's own cases each, drawn at random. There is no owner label for these,
  so they are measured against the role's own model only.
- `brief`: the 10 papers of the preference set, the owner's hand-read Focus papers.
- `writer`: five of production's daily digests, of 1 to 56 papers (circuits, cot-faithfulness and interpretability
  on 10-16; cot-monitoring and alignment-and-safety on 10-19).

**Refusals set, 23 papers × 5 roles.** The three refused so far, and 20 from the archive's search, five a theme:
biosecurity, chemical weapons and CBRN, cyber offence, jailbreaks. The chemical-weapons queries found CBRN and
capability-evaluation papers rather than chemistry. Each paper was graded for alignment-and-safety by `judge` and `check`, for language-models by
`field_grade`, and judged by `analyse` and `brief` in its theme's class.

Every count below is per cell. A difference of a few cases in 81 or 60 is noise; the n of 10 for `brief` and 5 for
`writer` shows only large differences.

## Results

Answered means a usable answer; the role's own model is first in each table. "As owner" is over the labelled pairs
answered; "as own" is agreement with the role's own model, over the cases both answered. Side is the line between
central or related and the rest. Weight and the overstatement line (whether a Brief says the abstract overstates)
are what a Brief is compared on.

| Role | Candidate | Quality: answered | As owner: Fit, side | As own model | Refusals set: answered | Cost an answer |
| --- | --- | --- | --- | --- | --- | --- |
| `judge` | Opus 5.5 | 81/81 | 62, 79 of 81 | – | 20/23, 3 refused | $0.0064 |
| | Sonnet 5.5 | 81/81 | 64, 78 | Fit 74, side 78 of 81 | 23/23 | $0.0030 |
| | GPT-6.1 Sol | 81/81 | 65, 79 | 71, 79 | 23/23 | $0.0029 |
| | Kimi K3 | 81/81 | 65, 78 | 73, 78 | 23/23 | $0.0026 |
| `check` | Muse Spark 1.3 | 81/81 | 65, 80 | – | 22/23, 1 invalid | $0.0004 |
| | DeepSeek V4.1 Flash 1017 | 81/81 | 66, 79 | 73, 78 | 23/23 | $0.0001 |
| | Gemini 3.8 Flash | 81/81 | 67, 79 | 77, 80 | 23/23 | $0.0027 |
| | DeepSeek V4 Pro 0813 | 81/81 | 65, 78 | 75, 79 | 23/23 | $0.0004 |
| | MiniMax M3.1 Flash, free | 0/81 | – | – | 0/23 | – |
| `analyse` | Haiku 5.5 | 60/60 | – | – | 21/23, 2 refused | $0.0004 |
| | GPT-6 Luna | 60/60 | – | Weight 47, contribution 58 of 60 | 23/23 | $0.0001 |
| | DeepSeek V4.1 Flash | 60/60 | – | 50, 57 | 23/23 | $0.0005 |
| `field_grade` | Haiku 5.5 | 60/60 | – | – | 23/23 | $0.0002 |
| | GPT-6 Luna | 60/60 | – | Fit 30, side 48 of 60 | 23/23 | $0.0001 |
| | DeepSeek V4.1 Flash | 60/60 | – | 34, 51 | 23/23 | $0.0007 |
| `brief` | Sonnet 5.5, high | 10/10 | – | – | 21/23, 2 refused | $0.1066 |
| | GPT-6.1 Sol, high | 10/10 | – | Weight 8, line 6 of 10 | 23/23 | $0.0940 |
| | Kimi K3, high | 10/10 | – | 5, 5 | 23/23 | $0.0310 |
| `writer` | Opus 5.5 | 5/5 | – | – | – | $0.0591 |
| | Sonnet 5.5 | 5/5 | – | – | – | $0.0290 |
| | Kimi K3 | 5/5 | – | – | – | $0.0121 |

Costs are as normal calls, not batched; production's batched calls cost half. Cost an answer is the cost of every
call to the role's requests, refused ones included, over the usable answers.

- **Refusals.** Seven, all from Anthropic models: `judge` Opus on 2610.13116 and 2610.21200 again (`bio`) and on a
  third, 2606.11150 (ABC-Bench, `bio`); `brief` Sonnet on 2610.17907 and 2606.30819 (`cyber`); `analyse` Haiku on
  2606.11150 and 2608.02684 (`bio`). No candidate from another lab refused anything, and every other candidate
  answered each refused case. Muse returned one answer that was not JSON (2608.02684); everything else
  answered.
- **`judge`.** The three candidates are as close to the owner as Opus is (64–65 of 81 on Fit against Opus's 62; 78–79
  on the side) and agree with Opus on 71–74 Fits and 78–79 sides. Nothing separates them.
- **`check`.** The same: every working candidate is within two pairs of Muse against the owner. Gemini is the
  closest to Muse (77 of 81) and answered all 23 of the refusals set. Google's filter, which showed in the Sonnet
  replay as a completion with no choices from Gemini 4 Argon, did not show here.
- **MiniMax M3.1 Flash is unusable.** OpenRouter answered each of the run's requests with 404, "No endpoints found
  that can handle the requested parameters". One request per parameter
  ([minimax-probe.md](2026-10-19-fallback-ladders/minimax-probe.md), by
  [minimax_probe.py](2026-10-19-fallback-ladders/minimax_probe.py)) shows which: a plain request, reasoning effort
  alone and `require_parameters` alone are answered; a strict JSON schema with `require_parameters`, which every
  role's request carries, is not. It can't be listed.
- **`analyse`.** Both candidates agree with Haiku on 47–50 Weights of 60. Haiku re-asked agrees with its own nightly
  answer on 51 of 60 ([baseline.md](2026-10-19-fallback-ladders/baseline.md)), so the candidates are a few cases
  below the role's own noise. Both lean to solid (Haiku's incremental is their solid 10 and 9 times). Both answered
  the two papers Haiku refused.
- **`field_grade` is the weakest.** Luna and DeepSeek agree with Haiku on 30 and 34 Fits of 60 and 48 and 51 sides.
  Haiku re-asked agrees with its own nightly answer on 48 Fits and 55 sides, and the candidates with that nightly
  answer on 29 and 35 Fits, 51 and 52 sides ([production.csv](2026-10-19-fallback-ladders/production.csv)). Both
  grade higher than Haiku: of the 60, Haiku's related became central 17 times for Luna and 11 for DeepSeek, its
  adjacent central or related 11 and 5 times. On the refusals set the shift goes the other way (Haiku's adjacent is
  their none, 14 and 19 times of 23), on the right side of the line. A Field grade has no rubric, and there is no
  owner label to say which is right.

  A Field grade does not decide whether a paper is on the Field's page (every paper in the Field's class is), but
  how it shows there: its level, and two below it in mixed lists. On the exact Fit both candidates are clearly below
  Haiku re-asked (35 and 29 of 60, against 48); read as R1's measure, that drops both and leaves the role no
  fallback. The list instead holds a fallback grader to the side of the line, as the proxy for whether the grade
  calls the paper relevant: DeepSeek keeps production's side on 52 of 60, against 55 for Haiku re-asked, three
  cases. The cost is the level: a fallback's related papers often show as central. And it is rarely asked: Haiku
  refused no `field_grade` case here, and none of production's 4,422 `field_grade` calls to 2026-10-19 failed
  ([baseline.md](2026-10-19-fallback-ladders/baseline.md), "Production's calls").
- **`brief`: Kimi is lenient.** It rated all 10 quality papers solid (Sonnet: 5 solid, 5 incremental) and drew the
  overstatement line on 1 of 10 (Sonnet: 6), and on 9 of the 23 refusals-set papers (Sonnet 18 of 21, Sol 14 of 23).
  Sol is closer (Weight 8 of 10, line 6 of 10; 17 and 15 of 21 on the refusals set) but also flags less than Sonnet:
  2 of 10 on the quality set. Sol is today's fallback; it stays, and its leniency is worth knowing when a Brief
  comes from it. For scale, Sonnet agreed with Opus on 33 and 31 of 37 in the Sonnet replay
  ([2026-10-16-sonnet-briefs.md](2026-10-16-sonnet-briefs.md)).
- **`writer`.** All 15 digests were written. GPT-6.1 Sol (medium effort, no calls; a lab none of the three writers
  is from) read them blind, each digest's three in an order shuffled per digest
  ([writer-read.md](2026-10-19-fallback-ladders/writer-read.md); the texts in
  [writer-blind.md](2026-10-19-fallback-ladders/writer-blind.md), the key at the end of
  [writer-pairs.md](2026-10-19-fallback-ladders/writer-pairs.md)). Unblinded, faithfulness and readability out of 5
  per digest:

  | Digest (papers) | Opus | Sonnet | Kimi |
  | --- | --- | --- | --- |
  | 23 circuits | 3, 4 | 3, 4 | 3, 4 |
  | 21 cot-faithfulness | 4, 5 | 3, 4 | 5, 5 |
  | 26 cot-monitoring | 5, 5 | 5, 5 | 4, 4 |
  | 24 interpretability | 4, 5 | 4, 5 | 3, 3 |
  | 30 alignment-and-safety | 4, 5 | 4, 5 | 3, 3 |

  The reader held all three to its own bar (every must-read covered, every qualifier kept) and passed only the
  smaller days for any of them; Opus fails it on the same three days as the others, so the bar measures the task,
  not the fallback. Against each other: no writer invented a pick or misread a day wholesale, and on the circuits
  day all three made the same overstatement. Kimi wrote the hardest prose on the two largest days (readability 3 and
  3, against 5 and 5 for Opus and Sonnet) and covered the fewest of interpretability's must-reads (7 of 14). Five
  digests and one reader can show a writer that fails outright, and none did; they can't rank Opus and Sonnet.

  Against Opus digest by digest (R1): Kimi's faithfulness is lower on three digests by a point each, higher on
  one (21) and equal on one (23); its readability lower on three (by 1, 2 and 2) and equal on two. The losses
  gather on the two largest days, 24 and 30, where the reader found Kimi dropping a qualification (a causal claim
  the request makes only for minimal transformers; OwlSight called a faithfulness detector). Opus's own 3 is on
  digest 23, where all three made the same error. With one reader and five digests, one point on three digests is
  within what a second reader or another day could move, and Kimi was the best writer of digest 21: it leans worse
  on large days, which this set does not show as clearly worse. The list keeps it last. It writes only after two
  failed digests of a topic in a row, both from Anthropic models, and the alternative then is no digest: a failed
  digest is retried every run and the last model is kept until one is published. Read as clearly worse, the list
  is Opus → Sonnet, and a topic whose digest both refuse stays unwritten until one doesn't.

## For P2

- **A Google filter has two shapes, and only one moves down.** A completion with no choices, the shape Gemini 4
  Argon's filter took in the Sonnet replay, is a provider error, which D4 never moves down: the step waits, and a
  night of it goes to Vergil as an OpenRouter outage. A `finish_reason` of `content_filter` is a refusal, which moves
  down. Gemini 3.8 Flash, last on `check`'s list, filtered none of 23 papers here, so the list keeps it; P2's tests
  should pin down both shapes.
- **Haiku refuses in `analyse` too** (2 of 23 here, both `bio`); the refusals seen until now were `judge`'s and the
  Brief's.
- **Opus is likely to refuse 2606.11150 in `judge`** when a Focus or Area topic next grades it.

## The lists for P2

Each list is the role's own model, then its fallbacks in order. `judge`'s and `check`'s share no lab (D2): Anthropic,
OpenAI and Moonshot against Meta, DeepSeek and Google.

| Role | List | Why |
| --- | --- | --- |
| `judge` | Opus 5.5 → Sonnet 5.5 → GPT-6.1 Sol → Kimi K3 | Unchanged: all three answered every case, Opus's three refusals included, and match the owner as well as Opus does |
| `check` | Muse Spark 1.3 → DeepSeek V4.1 Flash 1017 → Gemini 3.8 Flash | Unchanged: both answered all 104 and match the owner as Muse does; MiniMax has no structured output, DeepSeek V4 Pro adds no lab |
| `analyse` | Haiku 5.5 → GPT-6 Luna → DeepSeek V4.1 Flash | Unchanged: both answered Haiku's two refusals and agree with it about equally; Luna costs less |
| `field_grade` | Haiku 5.5 → **DeepSeek V4.1 Flash → GPT-6 Luna** | Swapped: both keep production's side of the line about as Haiku re-asked does (52 and 51 of 60, against 55) and grade higher; DeepSeek is the closer on the Fit (35 against 29), a small margin; rarely asked |
| `brief` | Sonnet 5.5, high → GPT-6.1 Sol, high | **Kimi K3 dropped** (R1): on ordinary papers it rated every paper solid and flagged 1 overstatement in 10; Sol answered both of Sonnet's refusals |
| `writer` | Opus 5.5 → Sonnet 5.5 → Kimi K3 | Unchanged: every digest written; Kimi leans worse on the two largest days, not clearly worse in five digests; last, it writes only after two Anthropic failures in a row, when the alternative is no digest |

## The fallbacks' answers on the refused papers

For each refusal, the answer the list above would use instead, and the other grader's answer where a grade was
refused. Every candidate's answer to every role of these six papers is in
[tables.md](2026-10-19-fallback-ladders/tables.md), "Answers on the refusals set".

**2610.13116, DRYLAB-Bench: Benchmarking the Biosecurity Risk of Agentic LLM Mutation Design.** `judge` refused by
Opus (`bio`), here and on the nights of 2026-10-17 to 19.

- `judge`, Sonnet: **related**. "The paper introduces a methodological safety benchmark that measures dangerous
  biological design capabilities of LLM agents, with a new risk score and safe ground-truth verification, which
  falls under safety evaluation methodology and dangerous-capability evals, though its focus is biosecurity rather
  than alignment training or monitoring." (`check`, Muse: central.)
- Brief, Sonnet: **solid**. "The abstract overstates: it credits the risk increase to BDTs, but the S0-to-S2 gain
  also includes extra revision rounds. The no-tool iteration control is too noisy to separate the two, and the
  shuffled-tool and non-LLM-optimizer controls were not run. The benchmark poses 30 single-substitution design
  scenarios in eight policy-derived risk categories to 15 frontier LLMs. …"

**2610.21200, Poisoning the corpora of protein design systems during pre-training and retrieval.** `judge` refused by
Opus (`bio`), here and on the nights of 2026-10-17 to 19.

- `judge`, Sonnet: **adjacent**. "The paper studies data poisoning and backdoor attacks on protein language models
  and retrieval-based protein generators, which is a security and integrity topic for biological models rather than
  aligning language models or evaluating whether they behave as intended." (`check`, Muse: adjacent.)
- Brief, Sonnet: **incremental**, no overstatement line. "Two data-only poisoning attacks on protein design systems
  are tested. In the first, 300M-parameter ProtGPT2-style and ProGen2-style models were pre-trained on UniRef50 for
  12B tokens. … All 24 poisoned runs ended at 94-100% ASR-90. …"

**2610.17907, SpecCloak: Probing Harmful Code Generation through Engineering Specifications.** Brief refused by Opus
in N6 (cyber); here Sonnet refused it too (`cyber`). `judge`, Opus: central.

- Brief, Sol: **solid**. "The abstract overstates: the 96.88% detection figure has no matching reported experiment;
  the detailed primary evaluation reports 98/100 attacks detected with 21/100 benign false positives. PseudoSpec
  rewrites harmful programming requirements as engineering specifications, and SpecCloak refines these prompts with
  generator–discriminator feedback before making one target-model query. On 104 matched RedCode-Gen tasks across
  five open-weight models, specification prompts retaining risk keywords reduce refusal from 74.04–100% to
  0–8.65%; …"

**2606.11150, ABC-Bench: An Agentic Bio-Capabilities Benchmark for Biosecurity.** `judge` refused by Opus and
`analyse` by Haiku (`bio`), here only.

- `judge`, Sonnet: **related**. "ABC-Bench is a dangerous-capability evaluation of LLM agents for biosecurity risk,
  which falls under safety evaluation of capabilities, though it is a domain-specific benchmark with limited
  methodological novelty beyond human baselines and wet-lab validation." (`check`, Muse: none: "a narrow domain
  benchmark … without contributing alignment methods or general evaluation methodology".)
- `analyse`, Luna: **solid**. "Introduces ABC-Bench, evaluating LLM agents on benign and dual-use biological tasks;
  agents exceed median expert baselines, and validated robot scripts assembled DNA successfully."
- Brief, Sonnet: **solid**. "The abstract overstates: Claude Sonnet 4.6, Claude Opus 4.6 and GPT-5.4 refused every
  Screening Evasion sample and Claude Opus 4 refused 90%, so those models have no usable score there and cannot be
  said to beat the median expert on all three tasks. …"

**2608.02684, A Blind Spot in Alignment: Quantifying Biosecurity Risks in Large Language Models.** `analyse` refused
by Haiku (`bio`), and Muse's `check` answer was not JSON, here only.

- `analyse`, Luna: **solid**. "Introduces SPIKE-Bench, a toxin-design evaluation benchmark and three-stage risk
  protocol, and audits 32 LLMs, finding functional risk tracks biological capability more than alignment."
- `check`, DeepSeek V4.1 Flash 1017: **central**. "The paper introduces a biosecurity safety benchmark and
  evaluation protocol, audits model refusal and functional harm, and proposes a mitigation classifier, making
  safety evaluation methodology and benchmark design its main question." (`judge`, Opus: central.)

**2606.30819, AI-Generated PowerShell Malware: An Experimental Framework and Dataset.** Brief refused by Sonnet
(`cyber`), here only.

- Brief, Sol: **solid**. "The abstract overstates: the headline scores describe the best native-precision model on
  212 detection-positive samples and measure triggered Sigma-rule overlap, not complete OS-event overlap. PSStrikes
  combines 817 newly curated, deobfuscated PowerShell scripts with 1,127 previously collected malicious one-line
  commands, pairing code with manually reviewed natural-language descriptions. …"
