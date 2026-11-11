# Deep reads on five real papers (P5, V7)

P5 of `docs/plans/deep-read.md` (D3): Deep reads of five real papers, written by the deep service, measured, and read
by the owner. Two are the spec's sketch papers and three are from the day's set, of different kinds. The five were run
three times, as what the first run showed was fixed. The owner reads the third run's reads, the ones this PR ships, and
that read is the merge gate.

Every number below but the container's comes from `2026-11-10-deep-reads/runs.json`: each attempt's usage, trace and
reviews, the `check()` calls in its trace, and the read's length by `deep/src/length.ts`. The reads themselves, all
fifteen, are in `2026-11-10-deep-reads/reads.jsonl.gz`; their figures are not committed (arXiv's licence).

How they were made. The deep image was built from this branch and run as a dev container under production's limits
(read-only root, all capabilities dropped, 1 GiB of memory, 128 pids, a 64 MB `noexec` `/tmp`). It was pointed at a
dev `serve` on a copy of production's database (`scripts/refresh-sandbox.sh --production`). The model was GPT-6.1 Sol
at high effort through the subscription, with the dev credential. Each run requested the five as the owner does
(`fulltext.deep.request`), and `litwatch fulltext --deep` worked them one after another. List cost is pi-ai's price for
the tokens; the subscription pays nothing extra.

| Run | Code | What changed before it |
| --- | --- | --- |
| 1 | P4 (b7da903) and P5's contents-list fix | nothing else |
| 2 | 0fb4386 | the length check measured in code and told to writer and reviewer, the cap included; each caveat stated once; TeX in `String.raw`; a budget of 100 cells and an hour |
| 3 | c0f0aa3 | the writer told its length and the aim of a third, but not the cap |

## The reads

Rounds give the reviewer's scores (faithfulness, clarity, depth, calibration), then ✗ and the structure checks that
failed, if any. Tokens in are those not read from the cache; peak context is the most the writer was sent in one
call, cached tokens included. Length is the read's text as a share of the paper's main text.

| Run | Paper | Outcome | Rounds | Min | Cells | Tokens in / out | List cost | Peak context | check() refused | Length |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2611.05344 | passed | 5/5/4/5 ✗ length, anchors, why_boxes, experiments · 4/4/4/4 ✗ anchors · 4/5/4/5 | 26.0 | 57 | 327k / 43k | $1.60 | 126k | 0 of 7 | 43% |
| 1 | 2610.21029 | passed | 5/5/4/5 ✗ length, anchors, why_boxes, experiments · 4/4/4/4 ✗ length, why_boxes · 4/4/4/5 | 25.0 | 58 | 492k / 43k | $1.91 | 136k | 1 of 9 | 32% |
| 1 | 2611.10620 | passed | 4/5/5/4 ✗ length, anchors, why_boxes, experiments · 4/4/5/5 | 16.8 | 39 | 214k / 32k | $1.11 | 144k | 1 of 5 | 45% |
| 1 | 2611.12675 | kept, failed | 5/5/5/5 ✗ length, anchors, why_boxes, experiments · 5/5/5/5 ✗ length · 5/4/4/5 ✗ length | 18.8 | 43 | 303k / 36k | $1.28 | 135k | 1 of 8 | 44% |
| 1 | 2611.12835 | kept, failed | 5/4/4/5 ✗ length, why_boxes · 4/4/5/5 ✗ length, anchors · 5/5/4/5 ✗ length | 21.0 | 52 | 345k / 40k | $1.56 | 156k | 2 of 9 | 46% |
| 2 | 2611.05344 | passed | 5/4/4/5 ✗ anchors, why_boxes, experiments · 4/4/5/5 ✗ why_boxes · 5/4/5/5 | 16.3 | 31 | 243k / 29k | $0.99 | 91k | 0 of 5 | 47% |
| 2 | 2610.21029 | passed | 5/5/4/5 ✗ anchors · 5/5/4/5 | 13.0 | 34 | 244k / 20k | $0.92 | 110k | 0 of 6 | 50% |
| 2 | 2611.10620 | passed | 3/4/4/3 ✗ anchors, why_boxes · 4/5/4/5 ✗ why_boxes · 5/5/5/5 | 19.0 | 36 | 229k / 33k | $1.07 | 110k | 0 of 8 | 58% |
| 2 | 2611.12675 | passed | 5/4/4/4 ✗ anchors, why_boxes, experiments · 5/4/4/5 | 14.0 | 37 | 160k / 24k | $0.80 | 105k | 0 of 11 | 58% |
| 2 | 2611.12835 | passed | 5/4/4/5 ✗ why_boxes · 4/5/4/4 ✗ anchors · 5/5/4/5 | 18.8 | 40 | 328k / 31k | $1.28 | 131k | 0 of 9 | 58% |
| 3 | 2611.05344 | passed | 5/4/4/5 ✗ anchors, why_boxes, experiments · 5/4/5/5 ✗ anchors · 5/5/4/4 | 23.0 | 36 | 254k / 37k | $1.16 | 113k | 0 of 7 | 42% |
| 3 | 2610.21029 | passed | 4/4/4/5 ✗ anchors, why_boxes, experiments · 5/5/5/4 ✗ why_boxes · 4/4/5/5 | 17.5 | 40 | 321k / 28k | $1.23 | 125k | 0 of 9 | 44% |
| 3 | 2611.10620 | kept, failed | 4/4/4/5 ✗ anchors · 5/4/5/4 ✗ anchors, why_boxes · 5/4/5/5 ✗ anchors, experiments | 21.8 | 41 | 229k / 39k | $1.17 | 116k | 1 of 10 | 49% |
| 3 | 2611.12675 | passed | 4/5/4/4 ✗ anchors, why_boxes · 4/5/4/5 | 13.5 | 32 | 167k / 25k | $0.78 | 116k | 1 of 8 | 53% |
| 3 | 2611.12835 | passed | 5/4/4/5 ✗ anchors, why_boxes · 3/4/4/4 ✗ anchors · 5/5/4/5 | 21.0 | 47 | 306k / 37k | $1.36 | 121k | 1 of 8 | 54% |

The papers: 2611.05344, A Circuit for In-Context Learning (a sketch paper); 2610.21029, SCARCE, read at v2 (a sketch
paper); 2611.10620, collusion probes, an evaluation critique with many short subsections; 2611.12675, interpretability
across models, with its theory in the appendix and no bibliography in its text; 2611.12835, deception games, theory and
RL, 199k characters with a 136k appendix. All five have LaTeX text; V6 read a PDF-only paper.

## What the runs show

- **Every read was written and stored, 15 of 15**, with 3 to 10 figures each. No run came near a budget: the most was
  58 cells and 26 minutes in run 1, and 47 cells and 23 minutes under the new budget of 100 and an hour. The
  container peaked at 535 MiB and 92 pids, against 1 GiB and 128 (`docker stats` every 15 seconds; not in
  `runs.json`). The writer's context peaked at 91k–156k tokens.
- **No `submit` was refused, and nothing was stood by.** `check()` refused 8 of 119 drafts:
  - four KaTeX errors, from TeX typed in a plain JavaScript string (`\f`, `\b`, `\v` taken as escapes): three in run 1,
    one in run 3 after the prompt asked for `String.raw`;
  - numbers the paper doesn't state once ("7.1%, 6.2%, 2.9%", which the writer then dropped);
  - "9550" on 2611.12835 in runs 1 and 3, the leading digits of a commit hash in a link to the paper's code. That
    refusal was wrong, and the number check now reads no number in a commit hash;
  - one section order.
- **Length.** The reviewer judged it by eye and failed reads the owner found the right length: in run 1 it failed
  2611.12675 on length in all three rounds, on length alone in the last two, scoring 5/5/5/5 twice, and called
  SCARCE's read, at 32%, "much closer to the paper's main-text length". Its findings named repetition as the cause,
  the same caveats in the findings, Claims, lenses and Limitations. Code now measures length and fails a read only
  past 60%.
  - Told that limit (run 2), the writer wrote up to it: three of five reads at 58%. Told only its length and the aim of
    a third (run 3), the reads came to 42–54%. Run 1, with neither, was 32–46%. Five reads a run is little, so how much
    of that spread is the writer and how much is chance is not settled.
- **The first draft fails structure checks on nearly every read**, the anchors and the WHY boxes most, and costs a
  review round. One read in run 3 (2611.10620) still failed on the anchors and experiments after two revisions.
- **Time and cost** fell from 17–26 minutes and $1.11–1.91 in run 1 to 13–23 minutes and $0.78–1.36 in runs 2 and 3.

## Owner's read

First look, at run 1's reads: the length is right, the reads the reviewer failed on length included.

The full read, of run 3's reads (2026-11-11): right for a reader who has read the paper and wants its details in one
place, hard to follow for one who hasn't. Facts come one after another in the paper's order, with no story first:
2611.12675's Setup opens on its list of eleven models and a WHY box on one model's layer depth before it says what the
experiment does. The reviewer scored clarity 4 and 5 on these reads; it reads with the paper beside it, so it cannot
see what a reader without the paper is missing. Length and diagrams pass. Readability is P6 of the plan, and automatic
reads switch on with it, not with this run.
