# Shared archive integration (0.9.0)

Daily intake archives cs.CL, cs.AI and cs.LG independently of relevance. Canonical
OAI metadata and raw version histories are joined with observation dates; RSS
replacements also produce durable archival intents. Unknown revisions remain
unknown until resolved, and metadata-only edits do not request duplicate bytes.
Newer unknown observations clear stale local revision identity. Existing screening
results are retained when old papers change.

Intake commits metadata, PDF/source intents and coverage checkpoints together.
The API runs an independent PostgreSQL outbox dispatcher: pending, delivered,
available, unavailable and error are distinct states. The archive service owns
actual acquisition and compression. Pending work survives restarts/outages; failed
remote jobs remain visible and require `paper-archive retry JOB_ID` after diagnosis.
`GET /archive/status` exposes coverage, backlog states, unresolved versions and lag.
An intake checkpoint proves discovery and durable intents, not finished downloads.

Tier-1 screening promotes PDFs to priority 0 (background is 100). Curated routing
and deep processing reuse archived versioned PDFs; queued requests converge on the
same job. Pending PDFs keep required Nextcloud attachments retriable. If the archive
is unavailable, direct curated retrieval uses the same file-based arXiv pacing.
All paths serialize connections, respect persisted Retry-After and space requests
by at least three seconds. A missing configured pacing directory fails closed.
The existing Zotero tier-1/2, tier-1 Nextcloud, selection/model settings, deep cards
and digests remain the curated pipeline. Archival-only work does not route papers.

## Configuration and package

- `LITWATCH_ARCHIVE_URL` and `LITWATCH_ARCHIVE_TOKEN_FILE`: authenticated internal API.
- `ARXIV_PACING_DIR`: shared writable pacing directory; Dell mounts SSD
  `/srv/arxiv-work/pacing` at `/var/lib/arxiv-pacing`. Both processes run UID 1000.
- `LITWATCH_ARCHIVE_WAIT_SECONDS` (60): bounded curated wait; a pending job does
  not trigger a second direct download.
- `LITWATCH_ARCHIVE_DISPATCH_SECONDS` (5): idle outbox interval.

The checked-in `vendor/arxiv_archive-0.2.0-py3-none-any.whl` comes from the private
arxiv-archive project; its wheel hash is pinned by uv.lock. The base package has
only requests plus a stdlib pacing module. PyArrow/zstandard are server extras,
not Litwatch dependencies. To refresh, build that project with `uv build --wheel`,
copy the wheel into vendor, then `uv lock --refresh-package arxiv-archive`.

## Dell deployment and rollback

Compose and host units live in homelab-config. The archive retains its loopback
listener; a systemd socket proxy exposes only the private Docker bridge gateway
172.21.0.1:8787, with the same API authentication. No public ingress is added.

A dedicated `litwatch-ssd` Buildx builder stores its state in a bind-backed volume
at `/srv/arxiv-work/buildkit`; the final runtime image still resides in Docker's
normal root storage. Builds are capped at 1.5 CPU and 2 GiB RAM. Build with:

```sh
docker buildx build --builder litwatch-ssd --load \
  --build-arg VERSION=0.9.0 --build-arg REVISION="$(git rev-parse HEAD)" \
  -t litwatch:0.9.0-archive-20260927 .
```

Pre-migration database dump: `/srv/arxiv-work/state/litwatch-pre-stage2.dump`.
Old image ID/tag: `/srv/arxiv-work/state/litwatch-pre-stage2-image.txt`.
Schema changes are additive. Rollback with the previous image override; retain
outbox/checkpoints for replay. Stop the archive worker too if reverting to a
Litwatch image that does not share pacing. Never restore over a live database.

Activation/pilot receipts are recorded below after live checks. Historical missing
PDF recovery, August backfill, search and the optional revision sweep remain later
stages in arxiv-archive/IMPLEMENTATION-PLAN.md.

## Activation receipt — 2026-09-27

- Litwatch implementation commits `054cece` and `d1dd9ea`; archive service `989f975`.
  Built from clean committed source; unrelated model-evaluation working edits were
  excluded. Production screen model remains `openai/gpt-5.6-luna`.
- 218 tests passed on the clean initial release with isolated PostgreSQL; the
  final delayed-version publication fix passed all 12 dispatcher tests. Archive
  package: 38 tests passed. Migration succeeded on a restored production database
  with 12,353 papers. No production restore or rescreen was performed.
- Pilot `2609.29446v1` (tier 4) produced durable metadata/PDF/source intents and
  complete archive jobs. Screening/Zotero fields stayed unchanged. API read-back
  parsed a 3,714,093-byte, 15-page PDF and a 3,312,796-byte source tar containing
  `spade_dfl_v1.tex` (93,035 bytes). Existing `2609.29266v1` also returned its normal
  1,068,623-byte PDF through the Litwatch container.
- Intake-only run 33 completed canonical and raw-history discovery for all three
  categories, starting 2026-09-27. OAI response boundaries were 19:36:38 UTC (cs.CL),
  19:36:48 (cs.AI), and 19:36:57 (cs.LG). This interval contained zero records;
  checkpoints and run details were recorded, not inferred from process startup.
  No paid screening, digest, Zotero, Nextcloud or Notion publication was invoked
  by the pilot. Routing behavior and outage/revision handling were fixture tested.
- Handoff: historical backfill must overlap September 27 and reconcile its own
  results against the now-active daily intake. This receipt does not establish
  archive coverage before that date; the historical campaign has not run.

Receipts and rollback artifacts live under `/srv/arxiv-work/state/litwatch-stage2-*`
and `/srv/arxiv-work/state/litwatch-pre-stage2.*`. The outbox progress endpoint may
lag completed archive jobs by up to its background reconciliation interval.
