# Decisions

## Carried from the first v1 (2026-10-08 to 2026-10-12)

Numbers are the first spec's (`docs/v1/SPEC.md` at e6940ea, section 9). Status says what this spec does with each.

| # | Decision | Status |
| --- | --- | --- |
| 1 | Zotero items from broad selections stay; v1 stops adding to them and deletes nothing | kept |
| 2 | Check model: Muse Spark, a third lab | kept (Focus `check`, Area escalation) |
| 3 | Radar picks are not saved automatically | kept, for Area picks |
| 4 | Reporting a miss from the phone; PWA share target | kept; the topic is asked at once (decision 44) |
| 5 | The PI's Notion view carries Watch topics only | kept, as Focus topics |
| 6 | One search box across litwatch and the archive | changed by decision 45 |
| 7 | Topic edits apply forward; re-runs are explicit and scoped | kept, and applies to taxonomy edits |
| 8 | The web app is a reading surface, not a console | kept as a principle; grades now show as colour (decision 35) |
| 9 | Issues, not day pages | superseded by decisions 39–41 |
| 10 | Look: Masthead | superseded; the look is the second pass's |
| 11 | Configuration is read-only in the web app | kept |
| 12 | Reactions and labels are separate records | kept |
| 13 | Takes are bound to text and rubric | superseded by decision 28 (Gists and Briefs are bound to the paper revision) |
| 14 | Which splits the status line counts | kept for Focus, with "straddles relevant" read as Fit related or above |
| 15 | An issue is a record | kept for digests (decision 40) |
| 16 | A paper in two Watch topics appears in both | kept for Focus digests and topic pages; one Gist and one Brief per paper |
| 17 | Thumbs on every paper | kept, with reasons per stage (decision 43) |
| 18 | Unsave outranks every other reason | kept |
| 19 | Announced once | kept for Focus digests |
| 20 | Quality is a calibration workspace | kept, questions changed ([feedback.md](feedback.md)) |
| 21 | Writer: Opus 5.5 | kept |
| 22 | No scheduled runs before cutover | superseded: the side-by-side stack runs its scheduler |
| 23 | Daily and weekly issues are separate | superseded by digests per topic and period |
| 24 | Triage stops after three failed nights | kept for classification |
| 25 | Bulk BibTeX does not wait | kept |
| 26 | Releasing | kept (N9) |

## Decided 2026-10-13, redesigning the core

27. **A detail-level ladder for every paper and digest.** Each rung answers "is it worth going deeper?" with more
    evidence: a paper's Mark, Gist, Brief, Deep card, then the paper; a digest's Mark, Gist, then the digest. Rung
    names are roles; a rung's length depends on how many items are scanned at once.
28. **Gists replace takes.** One Gist per paper, 20–30 words, about the paper and not any topic, for every paper in
    the collection; it replaces the per-topic take.
29. **Briefs from the full text for Focus papers.** Central or related Focus papers get a Brief written from the full
    text; others show the abstract. A Brief says when the full text does not support the abstract.
30. **A hand-written taxonomy.** Every paper is sorted into a tree of classes, one primary and any secondary. The tree
    is written by hand, edited in Settings, applied forward.
31. **Topics are followed classes.** Two concepts: the taxonomy (what a paper is) and topics (what the owner follows
    and gets digests for). A topic covers its class's subtree.
32. **Depths: Focus, Area, Field.** Watch, Radar and Pulse are renamed for what they are to the owner: the current
    research question, the research area, the wider field. The owner sets each topic's depth; it often follows tree
    depth but need not.
33. **Every paper is classified, by a decision model.** One request per paper; the model is chosen by comparing Luna
    Decisions, Jev and Haiku 5.5 on labelled papers (N1). Focus topics keep the keyword check and an uncertain band as
    recall insurance.
34. **Fit and Weight.** Fit is per topic (central, related, adjacent, none); Weight is per paper (landmark, solid,
    incremental). The grade is derived from both. Weight is judged from the abstract for every paper and again from the
    full text when a full-text pass reads it.
35. **Rarity as colour.** A paper's standing shows as a colour stripe in five levels; topic badges carry the topic's
    identity. In a mixed list rarity is the best over followed topics weighted by depth, with Weight as a floor; on a
    topic page it is that topic's grade.
36. **Facets, not more scores.** Contribution kind, code released, weights released are facts on a paper; nothing else
    is scored.
37. **Assurance by depth.** A paper is graded per nearest followed topic at or above each class it is a direct member
    of; Focus by two labs,
    Area by a strong grader checked by a decision model and escalated on doubt, Field by one cheap model. A parent
    topic shows a child topic's papers at the child's grade.
38. **A full-text queue.** Briefs, Weight re-checks of Area pick candidates, and Deep cards wait for the text and run
    as soon as it arrives; the archive fetches their source urgently. LaTeX text first, PDF when there is no source.
39. **Digests per topic and depth.** Focus: every central paper daily, related as lines, a weekly sum-up. Area: daily
    text with 1–2 picks from the must reads, weekly with 5. Field: weekly with 3. A topic with nothing new gets a
    "Nothing new" digest; a day arXiv announced nothing gets one line.
40. **Digests wait for full text, then are records.** Written after a bounded wait for the passes they depend on;
    never changed afterwards except to strike through what the owner set aside.
41. **The Digest tab's ladder and order.** History of days with digest Marks, a day with digest Gists and pick Marks,
    a digest, a paper. Opens on today. Topics keep one fixed order and their slot when empty. Weekly digests sit in
    their Monday and are pinned during their week.
42. **Deep cards** are automatic for Focus must reads and Area picks and on request for any paper, and place the paper
    among the collection's papers with links.
43. **Reactions carry a reason per stage**: wrong topic (classification), overrated and underrated (grading or
    Weight), not interesting and already knew it (no stage). Only wrong topic and great find on a Focus topic are the
    owner's word.
44. **Misses are owner-first.** The owner's answer takes effect at once; the pipeline runs in parallel and its
    disagreement is recorded, not applied.
45. **The archive ranks searches.** One box; unfiltered queries are ranked by the archive and decorated by litwatch;
    collection filters search litwatch's index; relevance is the default with a query. The archive's exact-title
    ranking and litwatch's re-sorting are fixed before the switch.
46. **The archive is part of the stack.** It ships with litwatch, starts empty, is the only downloader, keeps LaTeX
    text for all intake and PDFs for followed, saved or opened papers (configurable; the owner's instance too).
47. **The collection is the processed papers.** litwatch keeps only papers it processed, with the text as judged;
    every other paper is the archive's, with a paper page and Process / Deep read actions.
48. **Restart rather than migrate.** Amended 2026-10-14: v1 starts from an empty database. The two most recent weeks
    are read from the archive and run through the whole pipeline, unlabelled; no data is carried from 0.14 or the
    first v1. Every other paper is not processed and stays in the archive; the owner's earlier labels become the
    evaluation set (decision 55). Backfill, through the same archive intake over older windows, comes after v1.
49. **The archive's code moves into this repository** as `archive/`, deployed as its own container(s); its read-only
    web interface is retired once litwatch covers it. (Agreed by the owner 2026-10-13.) Amended 2026-10-14: the code
    moves as one squashed commit; the old repository keeps the history and the owner's instance records.
50. **Costs.** Month-to-date spend per provider; amber against a monthly budget; nightly runs never downgraded; hard
    ceilings on re-runs and backfills.
51. **The library.** Focus papers at relevant or above are saved automatically; everything else by hand. Zotero's
    "Tier 1" and "Tier 2" collections become "Must read" and "Relevant" at the switch.
52. **Order of work.** The core is redesigned before the switch, in two passes: this spec, then the UI sketches and
    design; no code until both are merged. 0.14 stays production meanwhile.

## Decided 2026-10-14, planning the build

The build plan is [`docs/plans/v1-build.md`](../../plans/v1-build.md).

53. **Weight is judged in the paper's own area.** One Weight per paper, judged against its primary class (what is a
    landmark in one area can be incremental in another); classification runs first, so analysis knows it. A
    per-topic Weight is added only if "overrated" reactions show papers from another area rated too high.
54. **Fit and Weight have one written definition per level** ([pipeline.md](pipeline.md), section 4), given to every
    model that judges them. Fit is how close a paper is to a topic: a topic's subject is its Fit definition, and a
    Focus topic's rubric spells out what central, related and adjacent mean for it. Weight is how much the paper matters: 0.14's
    must-read bar for the broad selections becomes its definition. Area and Field topics carry no rubric.
55. **Evaluation labels without hand labelling.** The owner's first-v1 labels, 0.14's CoT positives and the
    regression set measure Focus recall; 0.14's selection tiers are silver memberships of the matching classes; a
    stratified sample labelled by two models of different labs gives silver primary classes, and the owner settles
    only where they disagree (at most 20 papers). Each source is a cohort, never pooled. The set lives as files in
    `evals/labels/`, not in the live database.
56. **The build.** The first v1's deployment was stopped on 2026-10-14 (its database dumped first); the owner reads
    0.14 until the restart, and 0.14 keeps registering new papers with the archive. The archive's code moves in
    first; the web app is built from N2 onward, in parallel with the core.

## Open

- The grade table and rarity offsets are starting points, tuned in N2.
