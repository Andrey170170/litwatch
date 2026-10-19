# Clear prose: one reader for every text litwatch writes

Run on 2026-10-19, branch `feat/clear-prose`, on `clear_prose`, a copy of production made with
`scripts/refresh-sandbox.sh --production --db clear_prose`. Nothing was written to production; the replay deleted four
digests from the copy to compose them again (it now rolls that back).

**The question.** The owner found Briefs hard to read: VERITAS's (2610.23275) names five things the paper coins (a
"layer-by-phase grid", "anchors", "Map, Profile and Dynamics", a "replay model") without saying what any of them is,
then runs fourteen numbers. Gists and digests had the same faults in milder form: the paper's own names and labels
used as if the reader knew them ("Level 3.m untested", "on BashArena"), mechanism before purpose, flat lists of
equal items. The prompts named no reader, and the writing model has just read the paper.

**The change.** One voice and one reader in `litwatch.writing`, used by the `analyse` Gist, the `brief` role and the
`writer`: a researcher fluent in machine learning who has not read the paper, and six principles after Gopen and
Swan ("The Science of Scientific Writing") and the curse of knowledge. No order of content is imposed. A Brief is an
opening line, then one or two paragraphs. The Fit reason (`judge`) is left as it was: changing the grader's prompt needs a classifier
replay first.

## What was run

| Texts | Papers | Model | Before | After |
| --- | --- | --- | --- | --- |
| Briefs and their Gists ([papers.yaml](2026-10-19-clear-prose/papers.yaml)) | 12: the seven read on 2026-10-19, five more by md5 | Sonnet 5.5, high | production's Briefs, same model and text | `litwatch eval briefs --preferences evals/2026-10-19-clear-prose/papers.yaml --out evals/2026-10-19-clear-prose/round1.jsonl` (`round2.jsonl` to `round5.jsonl` for later rounds), text as passes send it |
| Gists from the abstract | 12: the three opaque ones read, nine more by md5 | Haiku 5.5 | production's | [replay.py](2026-10-19-clear-prose/replay.py) |
| Digests | circuits day 16 Oct, interpretability day 15 Oct, cot-faithfulness and alignment-and-safety weeks of 12 Oct | Opus 5.5, medium | production's | [replay.py](2026-10-19-clear-prose/replay.py), `digests.compose` |

**$7.46 for the Briefs** (five rounds, one rerun and one retry, by the harness's journal); the Gist and digest calls
were not journalled.

## Results

- **Round 1** ([round1.jsonl](2026-10-19-clear-prose/round1.jsonl)) read better but added explanation on top of
  detail: Briefs of 192–229 words, four rejected (three paragraphs, or over the 240-word guard), and the full-text
  Gist kept the coined names. **Round 2** ([round2.jsonl](2026-10-19-clear-prose/round2.jsonl)) tells the Brief to
  make room by leaving out secondary results and names, and the Gist to cut a name the reader cannot know first;
  the guards are 280 words and three paragraphs, the asked length unchanged. All 12 answered, 187–234 words, one in
  three paragraphs. The owner found the Gists and digests good, the Briefs still hard to get started on: the first
  paragraph opened with 80–115 words of setup.
- **Rounds 3 to 5** ([round3.jsonl](2026-10-19-clear-prose/round3.jsonl), [round4.jsonl](2026-10-19-clear-prose/round4.jsonl),
  [round5.jsonl](2026-10-19-clear-prose/round5.jsonl)) ask for an opening line that starts from the title and the
  Gist and says what the paper takes on, the setup following in the paragraphs. Asked inside the first paragraph
  (round 3) or capped at about 20 words (round 4), the line came back as a paragraph of its own and three of round 4's
  twelve were rejected as four paragraphs. Round 5 makes that the shape, an opening line and then one or two
  paragraphs, with a guard of four: all 12 answered, openings of 16–48 words (four of them two sentences, a problem
  then a question), Briefs of 200–249 words in three or four blocks. The owner found these good.
- **Weight**, Briefs: 10 of 12 as production in round 2 and in round 5, the same two solid → incremental both times
  (2610.23275, 2610.23525), so likely the change rather than chance: both are papers whose abstract overstates, and
  a Brief that says plainly what was shown may judge them lower. Gists from the abstract: 10 of 12, one
  solid → incremental, one incremental → solid, which twelve papers cannot tell from run-to-run variation.
- **The overstatement line**: 11 of 12 as production in each round; one new line each, on a different paper
  (2610.23256 in round 2, 2610.22874 in round 5).
- **Length**: Briefs run near the top of the asked 120–200 words or past it; the new Gists from the abstract are
  24–34 words (the hard limit is 30, the guard 45). Digests run somewhat longer.
- **New habits**: 6 of 12 Gists from the abstract open "The authors"; 5 of round 5's 12 Brief openings "The paper
  asks (tests) whether".

## For the owner

[pairs.md](2026-10-19-clear-prose/pairs.md) puts each text beside production's, the Briefs from
round 5 (built by [pairs.py](2026-10-19-clear-prose/pairs.py)). The owner read them on 2026-10-19 and accepted the
change.
