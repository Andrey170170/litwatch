# Planned shared archive integration

> Historical planning brief. Implemented in 0.9.0 (054cece–6cbcfd7); the shipped
> behavior, settings and rollback are in [ARCHIVE-STAGE2.md](ARCHIVE-STAGE2.md).

User direction: 2026-09-22. Planning only; no Litwatch runtime behavior changes
or deployment are made by this note.

Archive service plan: ../../arxiv-archive/CATALOG-SERVICE.md .
Historical backfill plan: ../../arxiv-archive/BACKFILL.md .

## Intended change

For essentially every new paper within configured CS/AI intake scope, upsert its
metadata into the shared catalog and durably request acquisition of its PDF,
plus available source under the agreed source policy. This is independent of
screening results: low-tier/unselected papers should still enter the catalog and
archive. It does not mean downloading every field on arXiv. Audit intake filters
so archival candidates are captured before relevance-based filtering; record the
exact category union in configuration rather than silently broadening it.

The archive service owns final paths, zstd compression, hashes and artifact state.
Litwatch is a client and does not implement a second bulk writer or expose a
shared writable archive directory. Historical and daily PDFs use the same
original-byte-preserving `.pdf.zst` representation. Clients retrieve normal PDF
bytes through the service when needed. Sources may be absent/PDF-only; record
that explicitly and allow PDF completion independently.

## Preserve the existing research workflow

Keep current screening/rubrics, tier assignments, confirmation, deep processing,
Notion/digests and existing retry/projection semantics intact. Keep Zotero for
bibliography/curated routing, including current tier-1/2 behavior. Continue the
existing tier-1 PDF download/copy into Nextcloud and existing attachment/link
behavior. Bulk catalog registration must not create a Zotero item or Nextcloud
copy for every archived paper. The global catalog supplements Zotero.

Do not make successful archiving a prerequisite for screening, digest publication,
Zotero or Nextcloud routing. A later archive-first PDF retrieval optimization may
reuse available bytes for the tier-1 route, provided its existing fallback works
when the service is unavailable. Do not replace curated Nextcloud PDFs with
`.pdf.zst` files or archive-only links as part of this integration.

Current implementation touchpoints to inspect when implementing: arxiv_source.py
(intake), pipeline/orchestration and db.py (persistence), api.py (router wiring),
Zotero/WebDAV routing and deep processing. README describes tier behavior; inspect
live source/tests before choosing exact hooks. This note is not a migration spec
for unseen production changes.

## Reliable incremental delivery

Persist a catalog/acquisition outbox with the ingested metadata transaction, or
an equivalently durable replayable record. Each item carries paper ID, observed
version, metadata/provenance, requested artifacts and acquisition policy version.
Use a stable idempotency key; overlapping fetches and retries must converge.

Track pending, delivered/accepted, artifact-available, retryable failure and
terminal/unavailable states separately. An HTTP acceptance is not a downloaded
PDF. Archive outages leave durable pending work and do not fail existing curated
routing. Metadata intake checkpoints may advance once metadata and the outbox
are durable; they must not discard failed intake or pending archive delivery.
Expose archive lag, pending counts and last successful delivery separately from
Litwatch's screening/digest status. Disabling the integration leaves pending
records resumable and preserves current pipeline behavior.

## Revision watching

Optional tracking is restricted to configured fields/topics or explicitly tracked
paper IDs, not all historical arXiv fields. OAI-PMH incremental metadata updates
can include replacements as well as bibliographic changes. Use arXivRaw version
history to compare latest known versus held versions before requesting new bytes;
do not treat every OAI datestamp change as a new PDF. Keep withdrawals and
unavailable versions explicit. Do not reintroduce old papers as new digest items
or trigger full rescreening solely because archival metadata changed.

Official reference: https://info.arxiv.org/help/oa/index.html . Preserve pacing,
Retry-After handling, checkpoints and fallback/replay behavior in current intake.
Coordinate acquisition rate limits through the shared archive worker.

## Historical ownership and handoff

The arxiv-archive project imports the pinned HF snapshot, recovers the 4,120
missing PDFs, reconciles the August coverage gap, and catches up through today
(2026-09-22 planning date; explicit cutoff at execution). Litwatch owns day-to-day
increments only. Do not run that historical population through screening/Zotero/
Nextcloud/digests automatically. Existing historical pending acquisition jobs can
continue in the archive worker without becoming Litwatch daily discoveries.

Agree an explicit completed watermark and overlapping intake interval at handoff.
Deduplicate by paper/version/artifact; do not assume the snapshot's last metadata
date proves complete PDF coverage or establishes a safe non-overlapping start.

## Focused acceptance checks for implementation

- A newly ingested low-tier paper is cataloged and queued for archival without
  receiving a new Zotero/Nextcloud projection from this integration.
- Tier-1 and tier-2 curated behavior remains as before, including tier-1 Nextcloud.
- Service outage/restart preserves pending delivery and normal digest/routing.
- Duplicate intake/delivery produces one canonical artifact; an accepted job is
  not falsely reported as available.
- A real watched revision queues the new artifact; a DOI-only edit does not.
- Historical catch-up overlaps safely with daily intake without replaying the
  historical selection through the recommendation pipeline.
