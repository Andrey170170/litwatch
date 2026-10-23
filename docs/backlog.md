# Backlog

Everything specified or agreed but not built yet, in the order it should be picked up. An item leaves this list
when it is merged; one that needs shaping first says so. The inventory of v1's unbuilt parts comes from an audit of
`6ace837` on 2026-10-22, checked against the code. Add to this list rather than leaving a gap only in a spec, a
plan's notes or a `Later` placeholder in the web app.

## Now

1. **Jobs.** Work outside the nightly run: Process runs a paper now, a topic's scoped re-runs with estimates and a
   ceiling, Run replay from Overview, a digest's Write again queued, and the Jobs page. Approved plan:
   [`plans/jobs.md`](plans/jobs.md), three stages.

## v1's unbuilt parts

Specified in [`specs/v1/`](specs/v1/README.md) and not built, or only partly. Nothing here is shaped yet; each needs
a short plan before it is built.

2. **The taxonomy write API.** Edit a class's name, description and threshold in place, with Save and the re-run
   offer (DESIGN.md, "Taxonomy"); Import YAML from Settings (only the CLI imports now); move a followed topic to
   another class (the topic save endpoint binds it to the URL's class). Re-runs after a class edit (classification,
   then grading) come with it, on top of Jobs. Placeholder: `web/src/routes/settings/taxonomy.tsx`.
3. **Correcting a classified paper.** Calibrate's "Classes right?" and adding or removing a paper's class membership
   (feedback.md). Owner memberships are stored already, but classify-by-hand only accepts papers awaiting
   classification. Placeholder: `web/src/routes/settings/calibrate.tsx`.
4. **Ratings on the generated text.** ✓/✗ on a Gist and a Brief, from Calibrate and from the paper's ladder,
   stored against the exact text rated (feedback.md, data.md's `text_ratings`). No table, endpoint or control yet.
5. **Misses: Resolved, and a record that keeps its diagnosis.** "Resolved" on a miss needs a write (DESIGN.md,
   "Misses"). The miss record should keep each stage's cells as they were at the report; today the trace is
   computed from current data, so a re-grade or a taxonomy edit rewrites the diagnosis.
6. **An integration's events.** The per-integration history (time, event, paper, result; all, failed or waiting)
   with Retry and Skip (DESIGN.md, "Integrations"). `/api/events` is a forward feed of changes with no delivery
   results. Placeholder: `web/src/routes/settings/integrations.tsx`.
7. **The sidebar's count for today.** The count beside a followed topic with a digest today, highlighted for Focus
   (DESIGN.md, "Shell"); `/api/summary` carries badges only.
8. **Today in the schedule's zone.** The Digest's defaults (today's Day, the last complete Week, History's pinned
   week) use the browser's date. `/api/status` or the summary should carry today's date; see
   [`notes/schedule-zone-today.md`](notes/schedule-zone-today.md).
9. **Smaller gaps:**
   - Unclassified papers listed on Papers, not only counted (the API can return them).
   - Code and weights links from the analysis, not only from a Deep card's URL.
   - Which source set the owner's word on a topic, from the API instead of guessed by the UI.
   - Typed response models for `/quality`, `/runs` and `/calibrate`.
   - The topic editor saying a re-run of its version was not run ("Version 4 has a re-run not run ›"), an open
     question from the settings sketch.
   - A Calibrate session whose drawn topic left Focus: answering it is a 422 now. *Needs a decision* on what an
     answer there means.

## After v1's plan ends

10. **The codebase review** of 2026-10-21: cleanups and second passes, triaged with the owner, none shaped yet.
    [`notes/codebase-review-2026-10-21.md`](notes/codebase-review-2026-10-21.md).
11. **Fold the archive into litwatch, then rebuild search** (one Postgres, all Docker; then search with prefixes and
    typos). Outline: [`notes/archive-fold-and-search.md`](notes/archive-fold-and-search.md); the search engine check:
    [`notes/pg-search-spike.md`](notes/pg-search-spike.md).
12. **N9, the release** ([milestones.md](specs/v1/milestones.md)): `v1.0.0`, install docs, repository clean-up,
    `evals/` and homelab specifics out of the repository.
13. **Backfill** of the archive's older papers, after v1 (milestones.md): a local decision-style model over several
    nights, then the stronger graders on what it surfaces. It builds on Jobs' scoped work and ceilings.

Smaller deferred follow-ups, each owned by its note: [`notes/deep-session-end.md`](notes/deep-session-end.md),
[`notes/updated-papers-follow-ups.md`](notes/updated-papers-follow-ups.md).
