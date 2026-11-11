# Deep reads on five real papers (P5, V7)

P5 of `docs/plans/deep-read.md` (D3): five Deep reads written by the deep service as built at P4 (b7da903), plus P5's
contents-list fix (e44fb71). Two are the spec's sketch papers and three are from today's set, chosen for different
kinds of paper. The owner's read is the merge gate, and it is pending until marked below.

How they were made. The deep image was built from this branch and run as a dev container under production's limits
(read-only root, all capabilities dropped, 1 GiB of memory, 128 pids, a 64 MB `noexec` `/tmp`). It was pointed at a
dev `serve` on a copy of production's database (`scripts/refresh-sandbox.sh --production`). The model was GPT-6.1 Sol
at high effort through the subscription, with the dev credential, and the budget was the P4 default (150 cells, a
120-minute wall clock). Each paper got an owner's request (`fulltext.deep.request`), and `litwatch fulltext --deep`
worked the five one after another. The numbers come from each attempt's `usage`, `trace` and `reviews`. List cost is
pi-ai's price for the tokens; the subscription pays nothing extra.

| Paper | Kind | Outcome | Rounds and scores (F/C/D/Cal) | Min | Cells | Tokens in / out | List cost | Peak input | check() refused | Stood by | Owner |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2611.05344 | interpretability, a circuit; sketch paper | passed | 5/5/4/5 ✗ · 4/4/4/4 ✗ · 4/5/4/5 ✓ | 26.0 | 57 | 327k / 44k | $1.60 | 42k | 0 of 7 | 0 | pending |
| 2610.21029 | multi-agent systems, v2; sketch paper | passed | 5/5/4/5 ✗ · 4/4/4/4 ✗ · 4/4/4/5 ✓ | 25.0 | 58 | 493k / 43k | $1.91 | 60k | 1 of 9 | 0 | pending |
| 2611.10620 | evaluation critique, many short subsections | passed | 4/5/5/4 ✗ · 4/4/5/5 ✓ | 16.8 | 39 | 215k / 33k | $1.11 | 40k | 1 of 5 | 0 | pending |
| 2611.12675 | interpretability, theory in the appendix, no bibliography in its text | kept, review failed | 5/5/5/5 ✗ · 5/5/5/5 ✗ · 5/4/4/5 ✗ | 18.8 | 43 | 304k / 37k | $1.28 | 78k | 1 of 8 | 0 | pending |
| 2611.12835 | theory and RL, 199k characters, a 136k appendix | kept, review failed | 5/4/4/5 ✗ · 4/4/5/5 ✗ · 5/5/4/5 ✗ | 21.0 | 52 | 346k / 41k | $1.56 | 34k | 2 of 9 | 0 | pending |

✗ marks a round the reviewer failed, ✓ one it passed. Every score was 4 or above in every round, so every failed
round failed on a structure check alone.

## What the runs show

- **Every read was written and stored**, with 3 to 10 figures each. No run hit a budget: the most was 58 cells and 26
  minutes, against 150 cells and 120 minutes. The container peaked at 531 MiB and 92 pids.
- **The code checks refused 5 of 38 `check()` drafts, and no `submit`.**
  - Three refusals were KaTeX errors: the writer typed `\frac` and `\bigl` in a plain JavaScript string, where `\f`
    and `\b` are escape characters.
  - Two were numbers the paper doesn't state ("7.1%, 6.2%, 2.9%"; "9550"). The writer dropped or restated them.
  - Nothing was stood by, so no refused place or number was the paper's after all.
- **The first draft failed the same structure checks on every paper:** the anchors, the WHY boxes, the findings as
  experiments, and the length. Each costs a review round.
- **Length is what the reviewer won't pass.** Both failed reads failed on length alone in round 3. 2611.12675 scored
  5/5/5/5 in rounds 1 and 2 and failed on length both times.
  - The reviewer judges length by eye, and not consistently. SCARCE's read is about a third of its main text, the
    target, yet round 1 called it "much closer to the paper's main-text length".
  - Measured roughly, each read is this share of its paper's main text: 44% (2611.05344), 32% (2610.21029), 47%
    (2611.10620), 46% (2611.12675) and 50% (2611.12835). The measure counts every string in the read and every
    character of the LaTeX from the first heading to the bibliography or appendix, markup included.
  - Every length finding names the same cause, repetition rather than detail: the same caveats restated in the
    findings, Claims, the lenses and Limitations. In the same reviews, the reviewer asks for more method and result
    detail.

## Owner's read

To be filled in from the owner's read of the five reads.
