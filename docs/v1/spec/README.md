# litwatch v1 specification

Status (2026-10-13): **redesign of the core, not built.** Milestones M0–M6 of the first v1 design are built on branch
`v1` and run side by side with 0.14 (`litwatch-v1` stack). Reading that build showed the design was wrong in two
places: the reading surface (a newspaper, not the scannable tool 0.14's Notion tables were) and the core (one way of
judging per topic kind, with nothing that tells a good paper from a weak one at a glance). This spec replaces the
first one. The first spec is `docs/v1/SPEC.md` at commit e6940ea; what it decided and this one keeps is listed in
[decisions.md](decisions.md).

Terms are defined once, in [`GLOSSARY.md`](../../../GLOSSARY.md) at the repository root. These files use them
without redefining them.

## Why litwatch exists

litwatch is insurance, not a reading list. Its owner follows a narrow research question and cannot afford to miss a
paper on it; the wider area matters as trends and a few papers a day; the field around that matters as a weekly
overview. Around this sits a continuously growing collection that should be usable later: searchable, citable,
readable at whatever depth is needed at that moment.

The owner reads by colour and position before text, and goes deeper one level at a time: a glance at a list, a
sentence, a paragraph, the full text or a deep card. Every page is built on that ladder.

## Goals

1. Never silently miss a paper on a Focus topic, and be able to show that.
2. Tell, at a glance, which papers deserve attention, in any list: a topic, a digest, a search.
3. Give each followed topic the depth of attention the owner chose: every central paper for Focus, the day's one or
   two for Area, the week's overview for Field.
4. Spend on models in proportion to how closely the owner reads a topic.
5. Be complete in itself: litwatch and the archive as one deployable stack, a model API key, nothing else.
6. Feel like 0.14 in use (dense, scannable, stable) but cleaner, faster and more pleasant.

## Not in v1

Multiple users. Backfilling the archive's older papers through the pipeline (v1 only makes it possible: every job
runs over any paper set or date range with a cost estimate first). Semantic search, citation counts and a citation
graph (they belong to the archive, later). Sources other than arXiv. Mobile apps.

## Files

| File | What it covers |
| --- | --- |
| [pipeline.md](pipeline.md) | What happens to a paper: intake, classification, analysis, grading, rarity, the full-text queue, revisions, failures, edits, volumes and cost |
| [digests.md](digests.md) | Digests per depth, daily and weekly, picks, quiet days, timing, the Digest tab's ladder |
| [feedback.md](feedback.md) | The owner's word, reactions, labels, misses, the library, Calibrate and the evaluation |
| [archive.md](archive.md) | The archive's role, deployment, storage policy, full text, search, papers outside the collection |
| [models.md](models.md) | Model roles, the gateway, lab diversity, assurance per depth, budgets |
| [integrations.md](integrations.md) | Events, the integration contract, Zotero, WebDAV, deep, webhook (mostly carried over) |
| [publications.md](publications.md) | Published versions on DBLP and BibTeX (carried over) |
| [data.md](data.md) | Modules, data model, configuration |
| [taxonomy.md](taxonomy.md) | The draft class tree |
| [milestones.md](milestones.md) | Build order and checks; what changes for M7 and M8 |
| [decisions.md](decisions.md) | Numbered decisions, old and new, and what is still open |

The web app's pages, look and components are not in this spec. They are the second pass (sketches in chat, then
`docs/v1/DESIGN.md` rewritten); the first v1 design (`DESIGN.md`, `design.html`) is superseded by it. What the
reading side must do is stated here where the mechanics need it (detail levels, rarity, digest order).
