# Updated papers in digests, and folding Needs you

Approved by the owner on 2026-10-20. This plan is the reference for two changes:
- **Updated papers in digests.** An old paper with a new arXiv version comes back once, in a folded list under its topic's digest papers.
- **Folding Needs you.** Each group on Needs you can be folded.

What litwatch does is set by the spec, [`docs/specs/v1/`](../specs/v1/README.md). This plan amended:
- [digests.md](../specs/v1/digests.md): the new section on updated papers, the record, and the tab;
- [DESIGN.md](../specs/v1/DESIGN.md): a digest, the digest card, and Needs you;
- [data.md](../specs/v1/data.md): the due rule and `digest_papers`;
- [decisions.md](../specs/v1/decisions.md): decisions 77–79.

Where this plan and the spec differ, the spec wins and this plan is corrected. Terms are the glossary's.

## Goal and approach

A digest lists only papers first announced within three weeks of its day. A paper that gets a new arXiv version months later never reaches a digest, even when it matters to a followed topic.

Intake takes in every changed record in its categories through OAI, including old papers litwatch never stored. On production in the week to 2026-10-20, that was 1,139 old papers with a new version. Their median first announcement was May 2026, and none had been in a digest.

The owner's words: an older paper "is good and interesting if highlighted. But it doesn't make sense to highlight one multiple times if it just keeps updating… digest is about recent stuff, if half year old paper is updated I wouldn't expect it to show up there (maybe only as collapsed 'updated papers' section)."

So:
- **Each old paper comes back once per topic.** It appears as an **updated** paper, in a folded list under the digest's papers. It costs no model call and has no place in the text.
- **Which papers come back depends on the topic's depth.** For Focus topics: central or related. For Area and Field topics: must read.
- **Nothing upstream changes.** Every new version is still taken in, classified and graded as today, and shows on its paper and topic pages. The plan only chooses which of them a digest lists.

Separately, Needs you's groups fold. A group of 66 Focus splits filled the phone screen many times over.

## Choices made without asking

- **An update's day.** It is the arXiv listing day of the newest version after the first. It is read from that version's submission time in `papers.version_history`, by the same rule as a first version's (`core/digests.listing_day`). A version with no submission time has no day and brings nothing back.
- **When a paper is old.** A paper is old when it was first announced (`papers.announced`) more than three weeks before that day. A recent paper's new versions never add a row, as today.
- **Daily and weekly work as for new papers.**
  - **Daily:** the row is in the topic's daily covering the version's day. If it arrives late, it is in the next daily, which shows its version's day.
  - **Weekly:** the row is also in that week's weekly.
  - **Field:** a Field topic has only weeklies, so its rows appear only there.
  - **"Once":** an updated row clears its own period only, so a daily's row is recapped in its week's weekly, as for new papers. A paper shown as new in either period never comes back as updated (D2). The due window is three weeks from the version's day, matching the window for new papers.
  - **Field weeklies** cover every eligible updated paper due on their Sunday (the three weeks ending then), so one graded after its week's weekly is in the next. A Focus or Area weekly covers its week's updates and any a published daily listed (written or "Nothing new") that no weekly has.
- **A paper waits for its grade.** A rewritten version is classified and graded again first. Its row comes only once the paper is eligible on its current text. Area keeps the Weight re-check gate it has for picks. A version with the same text needs no new grading.
- **Updated papers are a fourth role, `updated`, in `digest_papers`.** Each row is frozen as shown, like the other roles, plus the arXiv version it came back with. The role stays out of everything the existing roles feed:
  - the writer's input and its call;
  - the count of new papers and the best rarity;
  - picks;
  - a Focus weekly's earlier-paper candidates (`earlier_central`);
  - the evaluation input (`as_shown`);
  - the voice check's titles.
- **The row.** Each row shows the paper row's stripe, glow and title, with "v3 · first out May 2026" in place of its date, and no Gist (the owner's note: there are too many for Gists). The list sits under the papers, folded to "Updated · 3". It opens the way the Acknowledged section on Needs you does.
- **A day with only updated papers** publishes the "Nothing new" digest it would publish anyway, with the folded list under it. It still makes no model call. The digest card adds "· 3 updated" to its count line.
- **Fix an intake bug first.** When the arXivRaw version list fails to load (`versions_error`), `announced()` falls back to `published`, which can be `<created>`, a later version's date, and the upsert's `announced = coalesce(EXCLUDED.announced, papers.announced)` replaces the known first day with it. That would make an old paper due again as new. A day read from the first version's submission time is never replaced by one guessed from other metadata; the version history may still correct a guessed day either way (a Monday `<created>` reads as Tuesday's listing, a Monday-afternoon v1 is Wednesday's). data.md says so.
- **Needs you's folds** reuse the existing fold pattern (the Acknowledged section's `tpfold` and the shelves' session memory, `remember.ts`).

## Decisions

**D1: which old papers come back. Chosen: Focus central or related; Area and Field must read only.** Production's week to 2026-10-20, old papers with a new version:

| Depth | Topics | Eligible on the Focus rule (central or related) | Must read | Every graded member |
| --- | --- | --- | --- | --- |
| Focus | 3 | 12 | – | – |
| Area | 2 | – | 50 | 182 |
| Field | 4 | – | 293 | 860 |

That is about 4 rows a week per Focus topic, about 5 a day per Area topic, and about 70 per Field weekly.

The alternatives:
- **Focus only:** misses the must-read papers in the broad topics.
- **Every eligible paper, as first discussed:** about 13 rows a day per Area topic and 215 per Field weekly, a list nobody reads.

A relevant Area or Field paper that only got updated stays on its topic page (decision 78).

**D2: whether an appearance as new uses up the "once". Chosen: yes, one appearance per topic in all.** A paper that a digest of the topic showed as new never comes back as updated. One listed as updated comes back in no later daily, its week's weekly recapping it as weeklies do for new papers, and its later versions never bring it back. This matches "doesn't make sense to highlight one multiple times". In practice almost every updated row is a paper the owner never saw. The alternative was one updated row besides a paper's new appearance, for example a camera-ready version months later (decision 79).

**D3: how Needs you's groups fold. Chosen: the head folds it; a group of more than ten starts folded; the choice is remembered for the session.**
- Each group's head ("FOCUS SPLITS · 66") folds or opens it, and Acknowledge stays on the head.
- A folded group shows its head and its one line.
- The memory works like the topic page's shelves.

The alternatives were "the first 3, then Show all N" (still a scroll past every group's three) and "every group starts folded" (small groups take a tap to see).

## Risks

- **R1: Updated rows leak into what the existing roles feed.** The text, the counts, the weekly's earlier papers, `_COVERED` ("announced once"), `of_week`, `card_papers`, `as_shown` and `written()` all read `digest_papers`, by role or regardless of it.
  - **How it's handled:** each of them gets a test showing that an updated row stays out of it.
  - **The "once" guard:** it is its own query, so a row of one role never counts as the other's coverage. The exception is the D2 rule, where an earlier appearance as new does count.
- **R2: The volume is set by arXiv.** About 160 old papers a day arrive this way now. D1 sets how many become rows.
  - **How it's handled:** V2 measures the rows per topic on a copy of production before the web stage. A list is sorted by grade, then rarity, then the paper first out most recently. V2 found a Field weekly's rows all must read and epic, about 200 in language-models; the owner kept them all, newest first out on top (2026-10-20).
- **R3: A wrong day.** A version without a submission time adds no row rather than a guessed one, and the intake fix keeps a first day from moving later.
- **R4: The role's database constraint.** `digest_papers.role` has a CHECK constraint. The migration widens it, and the API's `DigestRole` and the web's `sections()` learn the new role. An unknown role would otherwise be dropped silently in the web app.

## Stages

| Stage | What it builds | Shows before the next starts |
| --- | --- | --- |
| P1 | Needs you's groups fold (D3). Web only and independent of P2 and P3, so it can ship first. | V4 |
| P2 | The intake fix; the `updated` role (a migration); its selection for daily and weekly digests and its guards; the API's version and first-out fields (OpenAPI types regenerated). | V1, V2, V3 |
| P3 | The digest page's folded updated list and the digest card's "· 3 updated". | V5 |

## Verification

By the method in [`docs/agents/verification.md`](../agents/verification.md).

- **V1. Which papers come back, and where.** An eligible old paper's new version gives one updated row, on its version's day, in the daily and in the week's weekly. For Field it is in the weekly only. These give no row:
  - a recent paper's new version;
  - a later version of a paper already shown;
  - a paper shown before as new, in a daily or a weekly;
  - an ineligible paper (Focus adjacent; Area or Field below must read);
  - a version without a submission time.

  A rewritten version waits for its grade, and Area waits for the re-check. A daily with only updated papers ("Nothing new") is recapped in its weekly; a Field update graded after its week's weekly is in the next one. *Evidence:* pytest on real Postgres, each test failing before the change.
- **V2. Nothing else changes.** No model call is added, and the text input, counts, best rarity, picks, earlier papers, `as_shown` and `_COVERED` are unchanged by updated rows. *Evidence:*
  - pytest, with the fake gateway counting calls;
  - digests written for one recent day on a fresh copy of production (`scripts/refresh-sandbox.sh --production`). It shows the same calls and the same writer input as without the change, and reports the updated rows per topic.
- **V3. The intake fix holds.** A failed version list no longer replaces a day read from the version history; a later version history still corrects a guessed day, in either direction. *Evidence:* pytests, the first failing before the fix.
- **V4. Needs you folds as D3 says.**
  - A group of more than ten starts folded and a smaller one open.
  - The head folds and opens the group, and the choice holds for the session.
  - Acknowledge works on a folded group.

  *Evidence:* vitest; a walk on a copy of production at 390 and 1440 px, with screenshots.
- **V5. The digest page shows updated papers as the spec says.**
  - The updated list shows folded under the papers and opens.
  - A "Nothing new" digest with updated papers shows them.
  - The digest card's count line says "· 3 updated".

  *Evidence:* vitest; a walk on that copy at 390 and 1440 px, with screenshots; the stack smoke test's pages.

## Scope

**In:**
- updated papers in daily and weekly digests;
- the intake day fix;
- the card's count;
- Needs you's folds;
- the spec amendments above.

**Out:**
- **Digest text about updated papers.** No model calls.
- **Remaking a Deep card for a new version.** That stays on request, as today.
- **An "Updated" shelf on topic pages, or updated papers in Notion.**
- **Changing which old papers intake takes in.**
