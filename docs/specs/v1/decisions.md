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
| 6 | One search box across litwatch and the archive | changed by decisions 45 and 59 |
| 7 | Topic edits apply forward; re-runs are explicit and scoped | kept, and applies to taxonomy edits |
| 8 | The web app is a reading surface, not a console | kept as a principle; grades now show as colour (decision 35) |
| 9 | Issues, not day pages | superseded by decisions 39–41 |
| 10 | Look: Masthead | superseded; the look is the second pass's |
| 11 | Configuration is read-only in the web app | kept |
| 12 | Reactions and labels are separate records | kept |
| 13 | Takes are bound to text and rubric | superseded by decision 28 (Gists and Briefs are bound to the paper revision) |
| 14 | Which splits the status line counts | superseded: the run status counts only failures; splits wait on Needs you (decision 72) |
| 15 | An issue is a record | kept for digests (decision 40) |
| 16 | A paper in two Watch topics appears in both | kept for Focus digests and topic pages; one Gist and one Brief per paper |
| 17 | Thumbs on every paper | kept, with reasons per stage (decision 43) |
| 18 | Unsave outranks every other reason | kept |
| 19 | Announced once | kept for Focus digests |
| 20 | Quality is a calibration workspace | kept, questions changed ([feedback.md](feedback.md)) |
| 21 | Writer: Opus 5.5 | kept |
| 22 | No scheduled runs before cutover | superseded: v1 takes 0.14's place, scheduler on after the switch night (decision 63) |
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
29. **Briefs from the full text.** Central or related Focus papers, and Area pick candidates through their Weight
    re-check, get a Brief written from the full text; papers without a written Brief show the abstract. A Brief says
    when the full text does not support the abstract.
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
    Superseded 2026-10-15 by decision 59.
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
    "Tier 1" and "Tier 2" collections become "Must read" and "Relevant" when Zotero is switched on after the
    switch (decision 66).
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
57. **The Gist follows the Brief.** A full-text pass that writes a Brief rewrites the paper's Gist in the same call,
    so the two never disagree and the text is read once. Every other paper keeps its Gist from the abstract
    (`analyse`, Haiku 5.5), which the N2 Gist evaluation checks.
58. **Deep card cost is observed, not budgeted.** Cards run on the `deep` service's own subscription; litwatch records
    each card's usage and shows the month's estimate beside gateway spend.

## Decided 2026-10-15, building N5b

59. **litwatch finds its own papers; the archive finds the rest.** Supersedes decision 45, as the archive contract
    ([archive.md](archive.md), Search) has it: a query searches litwatch's index for the papers it stores, and the
    archive's hits litwatch has not stored make their own band below them, in the archive's order. Collection
    filters search litwatch's index only. Relevance is the default with a query; the newest-first order asks each
    provider for its newest, and litwatch no longer re-sorts their hits.

## Decided 2026-10-16, model spend

The plan is [`docs/plans/model-spend.md`](../../plans/model-spend.md). The owner's caps: about $100 a month on the
Anthropic API and $20–30 on OpenRouter, against about $215 and $4 at the restart's rates.

60. **The nightly run's Anthropic calls are batched**, at half price, inside the gateway: the job decides, not the
    request, and the owner's actions are never batched. The nightly starts at 02:00 in the owner's zone and has a
    07:30 deadline, after which what is left goes as normal calls. OpenRouter's calls are not batched for now.
61. **`brief` runs on Sonnet 5.5**, after a replay of the restart's passes and the owner's read; a refusal is asked
    once of a model on OpenRouter from another lab, never again of an Anthropic model.
62. **The full text is read without its bibliography, and paid for once.** A pass that asks the identical question
    again (the same text, title, abstract and area, the journal's request key) is answered by the gateway's journal at
    no cost.

## Decided 2026-10-16, the switch

The plan is [`docs/plans/v1-deploy.md`](../../plans/v1-deploy.md).

63. **v1 takes 0.14's place**, its stack, paths and catalog entry, instead of running beside it; the two never run
    at once, so the deep credential moves without a new sign-in.
64. **The switch check runs on the catch-up week**: the week v1 reads from the archive on its first night is one
    0.14 has already processed, so the comparison needs no nights side by side.
65. **litwatch follows the homelab's telemetry convention**: JSON logs, Prometheus metrics with the shared job
    metrics read from the run records, and a `homelab.status/v1` status endpoint built from what the status line
    knows, on an internal port. The owner's instance sits behind the gateway's nginx and is instrumented in Vergil.
66. **Zotero and WebDAV are switched on after a few stable nights**, catching up from the first event: their
    cursors are created at the start of the outbox when the database is made. The collections are renamed then.
67. **Notion stops at the switch** until the owner has talked to the PI; the export is then rewritten against v1's
    events or removed. Settled by decisions 68–76.

## Decided 2026-10-19, the Notion export

The owner talked to the PI: she asked for a week to see whether the Notion view is useful to her. Settles decision
67. The export is described in [integrations.md](integrations.md), *The Notion export*.

68. **Notion is a built-in integration** talking to Notion's API, off unless configured and shipped with litwatch;
    n8n leaves the path and the first v1's workflow is removed. *Why:* the outbox already gives it a cursor, retries,
    failures under Settings and tests; an n8n workflow has none of these and lives outside the repository.
69. **Notion is the PI's view, not the owner's**: papers only, no digests; the exported topics and the minimum grade
    are options, by default the Focus topics at relevant or above. It follows grades, not the library. Pages stand
    on their own, with no links into litwatch. *Why:* the owner reads the web app; the PI cannot reach it.
70. **Plain words only**: Must read, Worth a look, No longer listed; TL;DR, Abstract, Deep read; short topic names;
    a "How to read this" page. *Why:* the PI does not know litwatch's terms, and Fit levels read as jargon. The
    page shows the abstract, not the Brief, until Briefs are written well enough (2026-10-19).
71. **A paper that leaves keeps its page**, marked No longer listed with a Why line per topic left, naming who took it
    off (the owner, litwatch on re-checking, or a change to the topic) and whether it was judged off-topic or less
    important; `grade.changed` gains a `cause` for this. Pages are never deleted. *Why:* a row the PI saw should not
    vanish without a reason.
72. **A grade per exported topic**, plus Grade as the best of them; topic views group by their topic's grade.
    *Why:* a paper can be a must read for one topic and worth a look for another.
73. **litwatch sets the database up once and then writes only its own columns and the page body.** Views and layout
    are the owner's; Status is the reader's; nothing comes back from Notion. *Why:* the owner arranges the views by
    hand, and a trial needs no feedback path.
74. **The Deep read is coloured callouts by section**, inside a collapsed toggle, as 0.14's reading card was. *Why:*
    the owner liked 0.14's look; the toggle keeps the page short for a reader who stops at the Abstract.
75. **The first set-up fills the database with every paper that qualifies then**, not only papers from new events.
76. **On the owner's instance the database goes on the existing LitWatch page**, already shared with the PI, behind
    its own Notion integration token. 0.14's Papers, Digest and Selections databases and the page's linked view go to
    Notion's trash once the owner has checked the new database; the Research workspace's Zotero-fed Papers is not
    touched.

## Decided 2026-10-19, failure handling

The plan is [`docs/plans/failure-handling.md`](../../plans/failure-handling.md). Vergil had pushed "papers stopped: 2
ungraded" for two biosecurity papers `judge` refused, and no new paper had a Deep card for days under the daily cap.

68. **A failure's reason decides where it goes.** The status document (Vergil) says when litwatch cannot do its job:
    a provider's account error or a night without one answered call, the Deep service failing, many papers stopped at
    one step in one night, and the checks before. A paper's own failure stays in the app, on Needs you and the run
    status.
69. **Every role that writes has an ordered list of fallback models**, chosen per role by a replay. A refusal goes
    down the list at once; an unusable answer one model per failed run; a call with no answer never. A fallback may
    come from the same lab (replacing decision 61's "never again of an Anthropic model"); `judge`'s and `check`'s
    lists share no lab. A step every model refused stops at once for the owner.
70. **A Focus grade one side refused throughout stands from the other lab**, marked one lab; central and related go
    to the owner.
71. **Deep cards have no daily cap.** A job that fails because of the service does not count against the paper; it
    pauses the queue and goes to Vergil. A re-run's passes are background work behind the night's.
72. **The owner acknowledges Needs you items** they have seen; acknowledged items stop counting in the run status,
    which counts only failures (stopped papers and passes, failed digests, a failed nightly), not splits. A failed
    nightly is not acknowledged.

## Decided 2026-10-20, updated papers in digests

The plan is [`docs/plans/updated-papers.md`](../../plans/updated-papers.md). Intake takes in every changed arXiv
record in its categories, about 1,100 old papers a week with a new version, and none of them reached a digest: a
digest lists only papers first announced within three weeks.

77. **An old paper's new version brings it back once**, as updated, in a folded list under the digest's papers, with
    no model call and no place in the text, the count of new papers, the best rarity or picks; the card counts it
    apart ("· 3 updated"). A digest is about recent work; an old paper worth a
    look is worth one line, not a second announcement.
78. **Only papers worth a look come back**: Focus central or related, Area and Field must read. With every graded
    member, a Field weekly's list would run to about 200 rows.
79. **Once per topic in all**: a paper a digest of the topic showed as new never comes back as updated, and one
    listed as updated comes back in no later daily, its week's weekly recapping it as for new papers; later versions
    never bring it back.

## Open

- The grade table and rarity offsets are starting points, tuned in N2.
