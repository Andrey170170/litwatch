# Clear prose: one reader for every text litwatch writes

Run on 2026-10-19, branch `feat/clear-prose`, on `clear_prose`, a copy of production made with
`scripts/refresh-sandbox.sh --production --db clear_prose`. Nothing was written to production; the replay deleted four
digests from the copy to compose them again.

**The question.** The owner found Briefs hard to read: VERITAS's (2610.23275) names five things the paper coins (a
"layer-by-phase grid", "anchors", "Map, Profile and Dynamics", a "replay model") without saying what any of them is,
then runs fourteen numbers. Gists and digests had the same faults in milder form: the paper's own names and labels
used as if the reader knew them ("Level 3.m untested", "on BashArena"), mechanism before purpose, flat lists of
equal items. The prompts named no reader, and the writing model has just read the paper.

**The change.** One voice and one reader in `litwatch.writing`, used by the `analyse` Gist, the `brief` role and the
`writer`: a researcher fluent in machine learning who has not read the paper, and six principles after Gopen and
Swan ("The Science of Scientific Writing") and the curse of knowledge. No order of content is imposed. A Brief may
take two paragraphs. The Fit reason (`judge`) is left as it was: changing the grader's prompt needs a classifier
replay first.

## What was run

| Texts | Papers | Model | Before | After |
| --- | --- | --- | --- | --- |
| Briefs and their Gists ([papers.yaml](2026-10-19-clear-prose/papers.yaml)) | 12: the seven read on 2026-10-19, five more by md5 | Sonnet 5.5, high | production's Briefs, same model and text | `litwatch eval briefs`, `--text pass` |
| Gists from the abstract | 12: the three opaque ones read, nine more by md5 | Haiku 5.5 | production's | [replay.py](2026-10-19-clear-prose/replay.py) |
| Digests | circuits day 16 Oct, interpretability day 15 Oct, cot-faithfulness and alignment-and-safety weeks of 12 Oct | Opus 5.5, medium | production's | [replay.py](2026-10-19-clear-prose/replay.py), `digests.compose` |

**$2.92 for the Briefs** (both rounds and one retry, by the harness's journal); the Gist and digest calls were not
journalled.

## Results

- **Round 1** ([round1.jsonl](2026-10-19-clear-prose/round1.jsonl)) read better but added explanation on top of
  detail: Briefs of 192–229 words, four rejected (three paragraphs, or over the 240-word guard), and the full-text
  Gist kept the coined names. **Round 2** ([round2.jsonl](2026-10-19-clear-prose/round2.jsonl)) tells the Brief to
  make room by leaving out secondary results and names, and the Gist to cut a name the reader cannot know first;
  the guards are 280 words and three paragraphs, the asked length unchanged. All 12 answered, 187–234 words, one in
  three paragraphs.
- **Weight**, Briefs: 10 of 12 as production, two solid → incremental. Gists from the abstract: 10 of 12, one
  solid → incremental, one incremental → solid. Twelve papers cannot tell these from run-to-run variation.
- **The overstatement line**: 11 of 12 as production; one new line (2610.23256).
- **Length**: Briefs run near the top of the asked 120–200 words or past it; the new Gists from the abstract are
  24–34 words (the hard limit is 30, the guard 45). Digests run somewhat longer.
- **A new habit**: 6 of 12 Gists from the abstract open "The authors".

## For the owner

[pairs.md](2026-10-19-clear-prose/pairs.md) puts each text beside production's (built by
[pairs.py](2026-10-19-clear-prose/pairs.py)). **The owner's read decides the change.**
