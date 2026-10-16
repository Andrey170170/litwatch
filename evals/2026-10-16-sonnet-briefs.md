# The Sonnet replay (model-spend plan, P1)

Run on 2026-10-16, branch `feat/brief-sonnet`, as evaluations on the scratch database `n6_restart` (evaluation tables
only, nothing written to verdicts). 40 of N6's papers ([papers.yaml](2026-10-16-sonnet-briefs/papers.yaml),
[select.sql](2026-10-16-sonnet-briefs/select.sql)): the three passes Opus 5.5 refused in N6 (one Brief, two in
grading), 12 Focus Briefs and 25 Area Weight re-checks, each in the area its pass judged Weight in. The reference is
Opus 5.5's answer from N6 ([opus.jsonl](2026-10-16-sonnet-briefs/opus.jsonl)); agreement with Opus is agreement,
not correctness. **$10.17 in all**, by the runs' own journals: Sonnet $9.64, the fallback candidates $0.53 (at least: Argon's call
with no answer has no known charge).

Each run is `litwatch eval briefs --preferences <set> --out <run>.jsonl --config <run>.toml --text <variant>` with
the configurations in [2026-10-16-sonnet-briefs/](2026-10-16-sonnet-briefs/); the tables are
[compare.py](2026-10-16-sonnet-briefs/compare.py)'s. `--text pass` is the text as passes now send it (bibliography
cut); `no-appendix` cuts the appendix too.

## Results

| Run | Answered | Refused | Weight as Opus | Overstatement line as Opus | Input tokens (median) | Output tokens (median) | Cost a pass | Cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Sonnet, high | 40 of 40 | 0 | 33 of 37 | 31 of 37 | 37,894 | 1,625 | $0.094 | $3.77 |
| Sonnet, medium | 40 of 40 | 0 | 32 of 37 | 23 of 37 | 37,894 | 704 | $0.085 | $3.40 |
| Sonnet, high, appendix cut | 40 of 40 | 0 | 32 of 37 | 29 of 37 | 22,021 | 1,501 | $0.062 | $2.47 |
| Gemini 4 Argon (the 3 refusals) | 2 of 3 | 0 | – | – | 33,619 | 5,857 | ≥ $0.084 | ≥ $0.25 |
| GPT-6.1 Sol (the 3 refusals) | 3 of 3 | 0 | – | – | 23,996 | 1,894 | $0.094 | $0.28 |

Agreement is over the 37 papers Opus answered.

- **Refusals.** Sonnet refused none of the 40, the three Opus refused included. The fallback will be asked rarely.
- **Weight**, Opus → Sonnet at high: incremental → incremental 11, solid → solid 22, incremental → solid 3,
  landmark → solid 1. Each of the four differences is one step: three up from incremental, one down from landmark.
- **The overstatement line**, at high: both 17, neither 14, Sonnet only 5, Opus only 1. At medium Sonnet flags 13
  that Opus does not, so medium both says less (704 output tokens against 1,625) and flags more.
- **Effort.** Medium saves 10 % a pass (the input dominates) and loses 8 papers of agreement on the overstatement
  line. High stays (D6).
- **The fallback.** Argon returned no answer for 2610.21200 (model-security; OpenRouter's completion had no
  choices, which is how Google's filter shows) and spent 5,857 output tokens a pass. Sol answered all three, at
  $0.094 a pass. The fallback is GPT-6.1 Sol at high (D5); MiMo was not needed.
- **The appendix.** Cutting it takes the median input from 37,894 to 22,021 tokens (−42 %) and the pass from $0.094
  to $0.062. Agreement moves by one paper on Weight and two on the overstatement line. But Briefs lose numbers and
  limits only the appendix has (below), so the appendix stays.
- **The bibliography.** The plan's third variant, the text as read with its bibliography, was not replayed: the cut
  was built before P1, so every run above already had it. Measured on the 40 texts without calls
  ([texts.py](2026-10-16-sonnet-briefs/texts.py)), 16 of the 40 LaTeX texts inline a `thebibliography` block; cutting
  it removes 5.9 % of all 40 texts' characters, and a median 10 % of a text that has one. Opus read the texts with their bibliographies,
  so the agreement above measures the cut and the model together.

## The appendix, side by side

GPT-6.1 Sol (medium effort, no model calls) compared all 40 pairs and checked every number or limit one answer had
and the other lacked against the paper's text, so that a difference two runs would show anyway is not counted against
the appendix ([appendix-read.md](2026-10-16-sonnet-briefs/appendix-read.md)). **Eight of the 40 Briefs without the
appendix lose a material item that only the appendix carries**, five if the three closest calls are left out. For
example:

- 2610.17457: the guard lowers the adaptive attack's success only from 93.55 % to 84.18 %, the one result combining
  its high detection F1 with adaptive attacks.
- 2610.12989: the SAE ablation raises degenerate outputs, and the narrativity drop survives excluding them: a
  confound and the check against it, both in the appendix.
- 2610.20221: harmful/benign labels agree with English only 92.6 % of the time, under a 99.6 % meaning-preservation
  score.

The plan's rule (an appendix is cut only if no Brief loses a number or a limit) fails, so **only the bibliography is
cut**, as passes do now (R5). The losses are a lower bound: the variant cut only from `\appendix`, and four papers
mark their appendix otherwise (`\beginappendix`, `\appendices`, an appendix part, "Supplementary Material").

## What it costs

At N6's rates (about 830 full-text passes a month; Opus 5.5 at $0.208 a pass, $172 a month), Sonnet at high is
about $78 a month as normal calls and $39 batched (with the appendix cut it would be $51 and $26). With the rest of
the Anthropic calls batched too, v1 comes to about $60 a month on Anthropic, under the $100 cap. P3's batched night
checks the batched half.

## For the owner

[pairs.md](2026-10-16-sonnet-briefs/pairs.md): the nine papers where Sonnet at high and Opus differ on Weight or the
overstatement line, each with both answers, then Sonnet's answers to the three Opus refused. **The owner's read
decides Sonnet** (V1).
