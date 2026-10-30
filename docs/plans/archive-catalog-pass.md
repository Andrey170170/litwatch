# The archive takes every new arXiv paper's metadata

Approved by the owner on 2026-10-30. This plan adds a catalog pass to intake: every night it registers in the archive
the metadata of each new or revised arXiv paper outside intake's categories, and a one-time run closes the gap since
the archive's seed. The spec, [`docs/specs/v1/`](../specs/v1/README.md), says what litwatch does. This plan amended
[pipeline.md](../specs/v1/pipeline.md) (Intake), [archive.md](../specs/v1/archive.md) (Deployment),
[data.md](../specs/v1/data.md) (the setting) and [decisions.md](../specs/v1/decisions.md) (decisions 88–90). Where
this plan and the spec differ, the spec wins and this plan is corrected. Terms are the glossary's.

## Goal and approach

The owner could not find "Back to Basics: A Skill-Centric Approach to Generalist Web Agents" (2610.35823, submitted
2026-10-26) in litwatch or in archive search. It is listed in cs.CV only, and intake reads cs.CL, cs.AI and cs.LG.

The archive should hold almost all of arXiv, and its seed did: a Hugging Face metadata parquet of 3.17M papers, ending
at 2608.27458 (submitted 2026-08-27). Since then the archive has gained only the papers intake registers. On
2026-10-30 its 2609 and 2610 ids held 11,334 and 12,540 papers, every one in an intake category. Its 2605–2608 ids
held 28.5k–32k each, about 20k of them outside intake's categories. Revisions of seeded papers outside intake's
categories are missing too. Archive search and `get_paper` therefore miss about 40k recent papers, and they get no
newer.

So intake gains a **catalog pass**: one more OAI-PMH harvest, over all of arXiv (no set), after the category passes.
It registers each record outside intake's categories in the archive as metadata only, and requests no files. The
backfill is the same pass started from 2026-08-20.

The owner's direction for the shape: build it into the systems that exist, not beside them. Metadata is light on
arXiv's API compared with downloads.

## Choices made without asking

- **Metadata only.** The pass requests no PDF and no LaTeX. Text comes when someone reads the paper: MCP reading and
  the paper page already request an archive-only paper's source urgently, and Process and Deep read work on it.
- **All of arXiv,** every archive and category, as the seed was: about 29k papers a month, a few MB of metadata.
- **Part of intake, nothing beside it.** The pass runs in `intake.collect` after the category passes. It uses
  intake's checkpoint table, its run record and its source seam (`PaperSource.fetch_category`). There is no new job,
  run kind, table or schedule.
- **Intake's categories are skipped by the catalog pass.** A record in any of them, cross-lists included, belongs to
  its category pass and the archive integration, which also request its files. The two never write the same paper.
  One harvest of all of arXiv could replace the category passes, but they keep their own checkpoints and Atom
  fallback. Keeping them costs about 40% more records a night.
- **Intake's OAI code, generalised.** `intake/oai.py`'s harvest takes "no set" and an optional `until`. The pass runs
  the canonical and the raw harvest, as `fetch_category(include_versions=True)` does. Its records therefore match the
  ones intake registers: split authors, full version history and the v1 date. Pacing is the shared arXiv pacer;
  arXiv's limit of one request every 3 seconds covers OAI too.
- **Registration through the existing `ArchiveClient.upsert_paper`**, one paper at a time (D3). The catalog's merge
  already keeps the higher version and the newer metadata, so revisions of seeded papers outside intake's categories
  are applied too. Deleted OAI records are ignored.
- **Its cursor** is a row in `intake_checkpoints` with category `*`. Intake's own checkpoint logic moves it, only
  after a complete window, with intake's inclusive overlap of one day. There is no Atom fallback: a failed night is
  harvested again from the checkpoint the next night.
- **Its outcome goes in the run's intake details, under its own key.** A failed catalog pass leaves the collection's
  intake `complete` and never fails the run. It adds a few pages and 1–3k registrations to the night's intake.
- **The backfill is the same code.** `litwatch intake --catalog-from DATE` lowers the `*` checkpoint and runs only the
  catalog pass. It walks one-week OAI windows up to today and moves the checkpoint after each one. It prints
  progress, can be stopped and resumed, and leaves the cursor where the nightly pass continues. It runs once at
  deploy, inside the `litwatch` container, from 2026-08-20: a week of overlap with the seed's end.
- **The setting:** `harvest = true` under `[integrations.archive]`. It does nothing while `register = false` (a
  litwatch running beside another one) or while the archive is off.
- **Unchanged:** the restart and the sandbox read from the archive (`intake/archive.py`) by intake's categories, so
  they never take these papers into the collection.

```
intake: category passes → catalog pass (* checkpoint → today)
  → OAI-PMH, no set, canonical + raw → skip intake categories and deleted records
  → PUT /v1/papers/{id} → archive catalog → its search index
```

## Decisions

- **D1. Where the harvest lives: in litwatch's intake.** The alternative was the archive's own command and systemd
  timer, writing the catalog in bulk with `discovery.py`'s parser. The fold
  ([notes/archive-fold-and-search.md](../notes/archive-fold-and-search.md)) drops `discovery.py` from the runtime and
  moves the systemd units into Docker, so that would be one more piece to port. In litwatch, the pass survives the
  fold unchanged: the PUT becomes a local write.
- **D2. Off by default; the owner's config turns it on.** archive.md's "a fresh archive starts empty and fills from
  intake" holds for other installs. Without a seed, all of arXiv from the install date onward is a half-truth that
  costs about 1k registrations a night.
- **D3. One paper at a time with the existing PUT; a bulk endpoint only if V3 says so.** 1–3k registrations a night
  cost nothing. The backfill's PUTs (70k–300k, R1) are each a SQLite transaction plus a search-index trigger. V3
  measures the time per 1,000 registrations. If the backfill would take over an hour, a bulk PUT is added, one
  transaction per OAI page: an endpoint, a client method and a catalog function. The fold drops the endpoint and
  keeps the function.

## Risks

- **R1. The backfill's size is unknown.** OAI's `from` is a metadata-change date, so the window also returns every
  older paper revised or re-stamped since 2026-08-20. arXiv's OAI reports no list size. Its pages are 1,300 records
  (measured 2026-10-30), and the total may be about 70k, or several times that if arXiv re-stamped records in bulk.
  The one-week windows, a checkpoint per window and printed progress make it stoppable and resumable. A larger total
  costs time, not correctness, because the upserts are idempotent.
- **R2. The backfill shares the arXiv pacer with the archive's downloads.** The effect is small: even 300k records is
  about 230 paced requests, roughly 15–25 minutes of slots shared with the worker. It runs by day, and urgent fetches
  wait at most one slot. The registrations, not arXiv, are the slow part (D3).
- **R3. It writes tens of thousands of records to the live catalog.** The catalog is backed up first (the archive's
  backup, a fresh export generation). Records only merge, so the worst case is redundant metadata on papers the
  archive already holds.
- **R4. Mixed record shapes.** A seeded record (Hugging Face keys such as `first_version_date`) is merged with a
  litwatch-shaped revision. This already happens every night for revised cs.CL/AI/LG papers. V4 checks a revised
  seeded paper outside intake's categories.

## Stages

| Stage | What | Shows before the next starts |
|---|---|---|
| P1 | One PR: the generalised OAI harvest, the catalog pass in intake, its `*` checkpoint, `--catalog-from`, the setting | V1–V3; the normal PR flow; merged |
| P2 | Deploy: back up the archive's catalog, set `harvest = true` in the stack's `litwatch.toml`, run `litwatch intake --catalog-from 2026-08-20` in the container | V4; the next night's run record shows V5 |

## Verification

| Claim | Evidence |
|---|---|
| V1. The catalog pass registers a record outside intake's categories as metadata only, with its version. It skips intake-category and deleted records, and moves the `*` checkpoint only after a complete window. The category passes and the collection are unchanged. | pytest through `intake.collect` with a fake `PaperSource` and a faked archive client: the PUTs made, no job requests, the checkpoint before and after a mid-window failure. |
| V2. A failing catalog pass leaves intake `complete` and does not fail the night. | pytest: the pass raises, intake's status stays complete, the run finishes with its digests, and the failure is in its intake details. |
| V3. It works against real arXiv. | One real day harvested into a disposable archive (`scripts/disposable-archive.sh`): the count registered, an empty job queue, a cs.CV paper of that day found through the archive's search, and the time per 1,000 registrations (D3). |
| V4. The gap is closed on production. | After the backfill, the 2609 and 2610 ids hold about as many papers outside intake's categories as 2607 and 2608 (about 20k a month). 2610.35823 is found by MCP `search_papers` and `get_paper`. The archive's queue has no jobs from the pass. A revised seeded paper outside intake's categories shows its new version. |
| V5. The nightly intake keeps it current. | The next night's run record shows the catalog pass complete and the `*` checkpoint at that day, and a paper outside intake's categories from that day is in the archive. |

## Scope

- **In:** the catalog pass in intake, for every new or revised arXiv paper outside intake's categories, as metadata;
  the backfill from 2026-08-20; the spec amendments above and the fold note's "fills from intake".
- **Out:**
  - files for these papers, which are fetched when read, as now;
  - holes inside the seed itself ("almost all" of arXiv), with no audit;
  - merging the category passes into the catalog pass;
  - processing these papers in litwatch, which stays the owner's Process.
