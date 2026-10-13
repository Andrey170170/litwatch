# Feedback, the library and evaluation

Feedback is the owner's corrections and the only ground truth litwatch has. The reading side offers it on every paper
and never asks for it; Calibrate, on the Quality page, asks, because the owner opens it to answer (first v1, decisions
8, 17 and 20). This file carries the first v1's rules where they still hold and changes what the new core changes.

## The owner's word

The owner's word on a paper for a topic outranks every verdict for that topic. It comes from a label, a
word-carrying reaction, or a reported miss, whichever is newest; an unsure label is no word. The effective grade
(see [pipeline.md](pipeline.md), section 4) is the owner's word where there is one.

## Reactions

A thumb on any paper, wherever it is shown, for the topic of the surface it is shown on (or none, in a plain search).
One per paper and topic, replaced on change, removable. Each reason points at the stage that would have made the
mistake, so corrections become per-stage error rates instead of one mixed signal:

| Thumb | Reason | Points at | Effect |
| --- | --- | --- | --- |
| down | wrong topic | classification | on a Focus topic, the owner's word: not a member (Fit none); elsewhere collected |
| down | overrated | grading or Weight | collected |
| down | not interesting | nothing (correct, but not for the owner) | collected |
| down | already knew it | nothing | collected, never counted as a correction |
| up | underrated | grading or Weight | collected |
| up | great find | nothing | on a Focus topic, the owner's word: relevant; elsewhere collected |

A reaction never writes a label (first v1, decision 12). The acknowledgement says what happened ("Marked not on CoT
monitoring; it leaves the library"). Collected reactions are shown on Quality per topic and stage over the trailing
30 days, for the owner or an agent to read and adjust classes, rubrics and thresholds by hand. Nothing retrains
automatically.

## Labels

Given in Calibrate or the label row on a paper page: for a topic, a Fit (central, related, adjacent, none) or unsure,
and for a class, member or not. Each label records how its paper was chosen (its origin) and keeps that origin when
its value changes (first v1 rules, carried). Unsure is excluded from every metric.

## Misses

A miss is a paper the owner found elsewhere that litwatch should have surfaced. Reporting one takes an arXiv link
(the phone's share target, first v1 decision 4) and the topic it belongs to, which is asked at once:

1. The owner's answer takes effect immediately: it is the owner's word (Fit central or related, the owner chooses) and
   a class membership for that topic's class, and the paper is saved.
2. In parallel the paper goes through the pipeline as any new paper (classification, analysis, grading for every
   followed topic it lands in), with source `miss`.
3. The miss record keeps what each stage concluded for the owner's topic: the class probability, the uncertain band
   and keyword check, each grader's Fit. Where the pipeline disagrees with the owner, the owner's answer stands and
   the disagreement is shown on the miss under Settings and counted on Quality. That record is what says whether
   classification or grading missed it.

## The library

Saved papers: every paper at an effective grade of relevant or above on a Focus topic, and anything the owner saves by
hand (an Area pick, a search result), except what the owner has unsaved. Save and Unsave are one toggle; the owner's
newest use of it outranks every other reason, including a later re-grade, until it is used again (first v1, decision
18). Unsaving is not a label. The library is what integrations act on; BibTeX can be copied for any paper.

## Quality and Calibrate

Quality, behind the settings gear, holds what needs the owner and how well the pipeline is doing:

- Focus splits, Area escalations, papers awaiting classification, ungraded topic memberships, failed or stuck
  full-text passes, failed digests, with actions;
- recall and precision per Focus topic over evaluation runs; collected reactions per topic and stage;
- the "matters, no topic" list (papers the owner said matter that fit no followed topic: a missing topic or a narrow
  class);
- cost against budget ([models.md](models.md)).

**Calibrate** carries over from the first v1 (one paper per screen, about 20 per session, half drawn from papers at
relevant or above and half from below, per Focus topic, with a recorded seed), with its questions changed:

| Question | Answers |
| --- | --- |
| Fit, for each Focus topic | central, related, adjacent, none, unsure |
| Classes right? | yes, or which class is missing or wrong |
| Value to the owner, whatever the topic | matters, fine, not for me |
| Gist and Brief | accurate, off |
| Note | free text |

## Evaluation

`replay` runs labelled and missed papers through the current classification, keyword check and grading, writing
only to evaluation tables, and reports per Focus topic over the papers the owner labelled central or related:

- **classification recall**: the classifier alone made the paper a member (at threshold);
- **membership recall**: classifier, uncertain band or keyword check made it a member;
- **grading recall**: graded related or above when graded;
- **end-to-end recall**: a member and graded related or above.

Classification recall judges the classifier on its own; the band and the keyword check must not hide its misses.
Precision at relevant comes from Calibrate's drawn samples only. Papers whose grading failed count as misses. These
are the first v1's four numbers with triage replaced by classification.

The old CoT verdicts, the provisional positives and every label given so far are the first evaluation set
([milestones.md](milestones.md), N2).
