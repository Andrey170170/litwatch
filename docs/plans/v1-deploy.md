# Deploying v1 in place of 0.14

Approved by the owner on 2026-10-16. This is milestone N8 of [`milestones.md`](../specs/v1/milestones.md), the
switch, as the owner reshaped it: v1 takes 0.14's place on the owner's server instead of running beside it. It is the
reference for the order of work, the choices made and how each stage is shown to work. What litwatch does is the spec,
[`docs/specs/v1/`](../specs/v1/README.md); this plan amends milestones.md (N8), integrations.md (Zotero) and
decisions.md (decisions 63–67). Where this plan and the spec differ, the spec wins and this plan is corrected. Terms
are the glossary's.

## Goal and approach

v1 is built (every PR of [`v1-build.md`](v1-build.md) and of [`model-spend.md`](model-spend.md) is merged into `v1`)
and the owner likes it. 0.14 still runs production: containers `litwatch` and `litwatch-deep` in the stack
`/opt/homelab-config/stacks/litwatch`, its database `litwatch` inside n8n's Postgres (`n8n-db`), scheduled by n8n,
read through Notion, writing to the owner's Zotero library and WebDAV.

The approach, in four stages:

1. **P1, telemetry.** A PR into `v1` so litwatch follows the homelab's telemetry convention
   (`/opt/homelab-config/docs/telemetry-convention.md`): JSON logs, Prometheus metrics with the shared job metrics,
   and a `homelab.status/v1` status endpoint. The owner wants litwatch properly inside the monitoring system.
2. **P2, the stack.** `stacks/litwatch` rewritten in v1's shape, behind the gateway's nginx as
   `litwatch.andrei-homelab.com`, in the Vergil catalog as instrumented, its Postgres in the nightly backups.
   Nothing is started.
3. **P3, the switch night and its check.** After 0.14's last daily run, 0.14 stops (kept intact) and v1 starts in its
   place on an empty database, with the last week read from the archive and processed by the 02:00 nightly. The next
   morning v1's Focus output on that week is checked against 0.14's, and the owner reads v1. If it holds, `v1` merges
   into `main`. Otherwise 0.14 comes back.
4. **P4, stable nights, then Zotero.** Two or three nights watched; then the Zotero collections are renamed and Zotero
   and WebDAV switched on, catching up from the first event. Then the owner removes 0.14.

Until Zotero is switched on, every step is reversible in minutes: 0.14's database, image, credentials and stack file
stay untouched (R1).

## Choices made without asking

- **0.14 stops before v1 starts; they never run at once.** Both stacks would want the compose project `litwatch` and
  the container names `litwatch` and `litwatch-deep`, and the deep credential must never be used by two processes
  (its refresh lock is per process). The switch check does not need both running: the catch-up week is a week 0.14
  has already processed. 0.14 is off for one night before the check; R1 covers bringing it back.
- **The stack is the first v1's stack again, renamed.** `stacks/litwatch-v1/docker-compose.yml` (homelab-config
  e1e49d3) already runs v1 with file secrets, a hardened container and its own Postgres. It moves to
  `stacks/litwatch` with the final names: project `litwatch`, containers `litwatch`, `litwatch-db`, `litwatch-deep`.
  0.14's compose file stays in git history (and as `docker-compose.0.14.yml` beside the new one until the owner
  removes 0.14) for the rollback. `deploy/compose.yaml` in this repository stays the self-host template; the
  owner's stack differs where his server already has the piece (the live archive, the gateway, file secrets).
- **The live host archive, not the bundled one.** The server already runs the archive as host services
  (`paper-archive*`, `http://172.21.0.1:8787`, 3.1M papers, installed from this repository's `archive/`). The stack
  reaches it there with `/srv/arxiv-work/catalog/api-token`, as 0.14 does. `[archive] pdfs = "followed"` is already
  the live archive's policy (`pdfs wanted`).
- **Runtime paths are 0.14's directory, new names inside it.** `/srv/appdata/litwatch/postgres` (the new cluster),
  `/srv/appdata/litwatch/secrets/` gains `db_password`, `api_token`, `read_token`, `anthropic_api_key` (copied from
  `/srv/appdata/litwatch-v1/secrets/`); 0.14's own files there (`postgres_password` and the rest) are not touched.
  `openrouter_api_key`, `s2_api_key`, `zotero_api_key` and the `webdav_*` files are shared, read-only.
- **The deep credential moves, it is not copied.** v1's deep mounts `/srv/appdata/litwatch/deep` (0.14's ChatGPT
  sign-in) only after `litwatch-deep` (0.14) has stopped. No new `login` is needed.
- **The address is `litwatch.andrei-homelab.com`.** The owner's own apps are named for themselves (`vergil.`,
  `meetings.`); `papers.` is already the archive's read-only search. An A record in the CoreDNS zone
  `andrei-homelab.com.zone`, a `conf.d/litwatch.conf` in the gateway's nginx with `papers.conf`'s allow-list (LAN,
  tailnet, the bridge gateway, the retained WireGuard range) and the HTTP-to-HTTPS redirect, and litwatch on the
  `gateway_gateway` network so nginx reaches it by name. The wildcard certificate covers it (valid to 2026-12-13).
  No `tailscale serve`, no published app port. The owner reinstalls the phone app from the new address.
- **Metrics and status on an internal port, 9110.** Published on the Docker bridge, `172.17.0.1:9110`, where
  Prometheus scrapes it (as Mediaforge's 9109). Never routed through nginx.
- **Job metrics are read from the run records at scrape time**, not kept in the process: a nightly started by hand
  with `docker compose exec litwatch litwatch nightly` runs in another process, and a restart must not reset them.
  `job_id` is `litwatch/<job>` for the scheduler's jobs (nightly, weekly, publications) and the full-text worker.
- **JSON logs by setting, on in the image.** `LITWATCH_LOG_FORMAT=json` (default `text`, as development reads it);
  the Dockerfile sets `json`. Uvicorn's own loggers use the same format; no access log lines carry tokens or
  request bodies.
- **The status components are what the status line already knows**, each with observed and expected values in its
  reason: the last nightly (failed, or not finished by 09:00 in the schedule's zone), the full-text queue (passes
  failed or awaiting text), digests (failed digests), each enabled integration (failing, or its lag), the archive
  (reachable), deep (reachable, when configured), and the budget (projected spend over a provider's budget is
  `degraded`). The schema is the homelab's; it is general enough to stay after the release (N9) and is documented
  in the repository.
- **Zotero and WebDAV hold their place from the first event.** Right after the migrations on the empty database,
  before any event, one statement creates their cursors at the start:
  `INSERT INTO integration_cursors (name, position_xid, position) VALUES ('zotero', '0', 0), ('webdav', '0', 0);`.
  Nothing reads them while the integrations are off; when they are switched on the existing row wins over "start at
  the current end" (`store.outbox.cursor`), and the outbox is never pruned. A runbook step, not code: it happens once.
- **The catch-up week is read from the archive.** `litwatch intake archive --since 2026-10-09 --until <the day before
  the switch night>` (no model calls), then the scheduled 02:00 nightly classifies, analyses, grades and works the
  full-text passes of every due paper in batches (decision 60), and writes the switch night's digests. The past days'
  digests follow in the morning with `litwatch digests write --since 2026-10-12 --until <the switch day>`. Expected
  cost: about $20 for about 3,300 papers at P3's measured $6.19 per 1,000, plus about $1–2 of digests; a watchdog as
  N6's (`evals/2026-10-16-restart/cap.sh`) stops at $35, and the owner is asked before going past it.
- **Notion is off.** No webhook integration at the switch; 0.14's n8n workflows are deactivated. The owner talks to
  the PI on Monday 2026-10-19 about whether she uses the Notion view; the export is then rewritten against v1's events
  or removed (R6).
- **The first v1's stack is retired with 0.14.** `stacks/litwatch-v1` and its stopped containers go when the owner
  removes 0.14; its database dump stays in `~/backups/`.
- **`v1` merges into `main` once the check passes** (P3), as a merge commit so `v1`'s history is kept; the workflow
  profile's base branch becomes `main` again. N9 (the release) stays separate.

## Decisions

Settled with the owner on 2026-10-16:

- **D1. In place of 0.14, not beside it.** v1 takes the stack, the paths, the stack unit, the catalog entry. *Why:*
  nothing named `-v1` is left after the switch, the deep credential moves without a new sign-in, and OpenRouter
  (about $20 left) does not pay for 0.14's nights during an overlap. Not taken: the spec's few days side by side.
- **D2. The switch check runs on the catch-up week.** v1's Focus output for the papers of 2026-10-09 onward must
  contain everything 0.14 put at tier 1–2 on the two CoT selections for the same papers, or each difference is
  looked at with the owner. *Why:* the week exists in both already; the check needs no parallel nights. Not taken:
  2–3 nights side by side.
- **D3. Behind the gateway's nginx, named among the owner's apps, in Vergil as instrumented.** *Why:* the owner's
  other services live there; phone access needs HTTPS, which the wildcard certificate gives. Not taken: the
  tailnet address of the first v1.
- **D4. Zotero after the system is stable, with the catch-up held from the first event.** *Why:* Zotero writes are
  the one step that is not reversible in minutes. Not taken: Zotero on at the switch.
- **D5. Notion stops at the switch** until the owner has talked to the PI.
- **D6. `v1` merges into `main` at the switch.** *Why:* `main` is what runs in production.
- **D7. `/status` is in scope**, not optional: the owner wants proper integration into the monitoring system.

## Risks

- **R1. The check fails, or the night goes wrong.** Rollback: stop the v1 stack, put back 0.14's compose file, start
  the stack unit; re-activate the n8n workflows. 0.14's database, image, deep credential and secrets were not
  touched, so 0.14 resumes; its intake looks back over the days it missed (checked before P3: `LITWATCH_INTAKE_SOURCE
  = oai` and its lookback). Before Zotero is on, v1 has written nothing outside its own database except archive
  registrations (deduplicated by the archive) and text fetches.
- **R2. The catch-up costs more than estimated.** The watchdog stops at $35; the journal is read per step. A nightly
  started outside 02:00–07:30 does not batch, so the catch-up runs only in the scheduled window.
- **R3. The budget line goes amber from the catch-up.** The new database's month to date includes the catch-up, and
  October's other spend is outside it. Amber only colours the status line (models.md, budgets); it is expected for
  October and noted for the owner, not tuned away.
- **R4. Zotero duplicates or losses at switch-on.** The integration reads each paper's current library state when it
  handles an event and finds an existing item by arXiv id or exact title before creating one, so the replay converges
  on the library as it stands; it removes only items it created. The collections are renamed before switch-on so
  nothing lands in a third set. One resumed WebDAV upload's md5 and mtime are checked against the real library once
  (carried from N8).
- **R5. Monitoring reads wrong.** A failed nightly must read as failed (`last_run` newer than `last_success`, `running`
  0) and a stale one as degraded; P1 shows both with tests, P2 in Vergil.
- **R6. Notion goes stale for the PI.** Her view stops updating at the switch. Acceptable until Monday by the owner's
  word; the n8n workflow and its README are rewritten or removed after his talk with her.
- **R7. Two writers on the archive.** 0.14 registers papers with the archive today. After the switch v1 does
  (`[integrations.archive] register` on); with D1 they never overlap.

## Stages

P1 is a PR into `v1`, by [`docs/agents/workflow.md`](../agents/workflow.md); P2 commits in homelab-config; P3 and P4
change production and are done with the owner present for the steps that need sudo (the stack unit, the gateway).

| Stage | What | Shows before the next starts |
| --- | --- | --- |
| P1 | **Telemetry PR.** JSON logs; a metrics listener (`litwatch serve --metrics-port`, off when unset) serving `/metrics` and `/status`; the `homelab_job_*` gauges from the run records; a few service gauges with bounded labels (full-text passes by state, each enabled integration's lag and failing, month-to-date spend and budget per provider); the `homelab.status/v1` components above; the spec amended where configuration and operations are described. | Checks green. pytest: a failed and a stale nightly read right in both endpoints; the exposition parses; no unbounded labels. `/metrics` and `/status` on the sandbox, read with curl. |
| P2 | **The stack.** `stacks/litwatch` in v1's shape (images built from `v1`'s head after P1), `litwatch.toml` (categories, schedule with its zone and deadline, `[archive]` at the live archive, `[integrations.archive]` with registration on, `[integrations.deep]`, no Zotero, WebDAV or webhook), the secrets, the DNS record and nginx route, `service.yaml` (instrumented: the stack unit, `litwatch` with its metrics, `litwatch-db`, `litwatch-deep` not required; a status-endpoint check; depends on gateway and paper-archive, no longer n8n), a sixth dump unit in `hosts/dell/db-backups` for `litwatch-db`, the scrape targets regenerated, the stack's README. Nothing started. | `docker compose config` resolves; `vergil catalog validate /opt/homelab-config` passes; `nginx -t` in the gateway container; the zone serial bumped; the owner reads the diff. |
| P3 | **The switch night.** After 0.14's daily run: dump 0.14's database once (`~/backups/`); deactivate 0.14's n8n workflows; stop 0.14; reload the gateway and CoreDNS; start v1; migrations, the cursor statement, `taxonomy import`; `intake archive` for the window; the 02:00 nightly under the watchdog. In the morning: the past days' digests; the switch check; the owner reads v1 at the new address. If it holds, merge `v1` into `main`. | The check written up in `evals/` (counts and cost per step, the 0.14 tier 1–2 CoT papers each found or explained); digests ready before 09:00; Vergil shows litwatch healthy; the owner's word. |
| P4 | **Stable nights, then Zotero.** Two or three scheduled nights watched (cost against $6.19 per 1,000, digests before 09:00, no failed steps left unexplained). Then: rename "Tier 1" and "Tier 2" to "Must read" and "Relevant" in the owner's library, add `[integrations.zotero]` and `[integrations.webdav]`, restart. The owner removes 0.14 and the first v1's stack. | The Library page before; Zotero's cursor caught up with no failures after; no duplicate items for a sample of papers already in the library; the WebDAV md5 and mtime check once. |

## Verification

| | Claim | Evidence |
| --- | --- | --- |
| V1 | litwatch's metrics and status say what happened, across processes and restarts (P1) | pytest on PostgreSQL: a nightly finished, failed, running and stale, read through both endpoints; the exposition parsed by `prometheus_client`'s parser; curl on the sandbox |
| V2 | The stack is valid before it runs (P2) | `docker compose config`, `vergil catalog validate`, `nginx -t` |
| V3 | v1 from nothing produces a readable week (P3) | counts and cost per step from the journal; every Focus central paper with its Brief or the reason it has none; the window's digests |
| V4 | v1 holds what 0.14 found on the CoT selections (P3, D2) | the 0.14 tier 1–2 papers on the two CoT selections for the window's papers, read from 0.14's database, each in v1's Focus output or looked at with the owner |
| V5 | Monitoring sees v1 (P3) | Vergil's litwatch card, Prometheus target up, the job metrics after the first nightly |
| V6 | Nights are stable and cost what was projected (P4) | each night's cost per 1,000 papers from the journal; digest times |
| V7 | Zotero catches up without duplicates (P4) | the cursor's lag to zero with no failures; a sample of items that existed before switch-on, each still one item, now in the renamed collection; the WebDAV check |

## Scope

**In:** P1–P4 as above; the spec amendments in N8, integrations.md and decisions.md.

**Out:** the Notion export (after the owner's talk with the PI, R6); the favicon pass; N9, the release (compose file
and docs for others, clean-up, licence, `v1.0.0`); the backfill of older papers; removing 0.14 and the first v1's
stack, which the owner does by hand.
