# The owner's server

This checkout lives on the server that also runs litwatch in production. Production, the v1 deployment and
development share one Docker daemon and one process table.

## What runs here

| Name | What | Where |
| --- | --- | --- |
| containers `litwatch`, `litwatch-db`, `litwatch-deep` | v1, in production since 2026-10-17; 0.14 stopped on 2026-10-16, kept until N8's P4 (its database dumped to `~/backups/litwatch-0.14-2026-10-16.dump`) | stack `/opt/homelab-config/stacks/litwatch` (its README) |
| containers `litwatch-v1`, `litwatch-v1-db`, `litwatch-deep-v1` | the first v1's deployment, **stopped** since 2026-10-14 (not removed; its database dumped to `~/backups/litwatch-v1-first-build-2026-10-14.dump`) | stack `/opt/homelab-config/stacks/litwatch-v1` (its README) |
| container `litwatch-v1-dev` | development Postgres | 127.0.0.1:55432; databases `litwatch` (an adopted copy of 0.14's production), `test` (pytest), `sandbox` (end-to-end runs, below) |
| user unit `litwatch-sketches` | the UI sketches | port 8791, serving `docs/specs/v1/sketches/` |

Production and the stopped v1 deployment are touched only when the task is about them; production's database is
also read, never written, to copy it into the sandbox (below).

## Running litwatch in development

```sh
set -a; . ./.env.v1; set +a
env -u ANTHROPIC_BASE_URL uv run litwatch …
```

- `.env` in this checkout points at the **production** database, and every `litwatch` command loads it for any
  variable not already set. The gitignored `.env.v1` sets `LITWATCH_DATABASE_URL` (dev `litwatch`),
  `TEST_DATABASE_URL` (dev `test`), `LITWATCH_API_TOKEN`, and the host archive: `LITWATCH_ARCHIVE_URL`
  (`http://127.0.0.1:8787`) and `LITWATCH_ARCHIVE_TOKEN_FILE` (`/srv/arxiv-work/catalog/api-token`, the file
  production's stack mounts). Source it first so `.env` only fills in the API keys.
- Database commands can apply this checkout's migrations automatically; `serve` does. Read-only evaluation commands
  opt out. Point one only at a database built for this checkout's schema or one behind it: a stale copy can fail
  halfway through and stay half-migrated.
- The agent shell exports `ANTHROPIC_BASE_URL` for the agent's own proxy. The Anthropic SDK picks it up and every
  litwatch call fails with 401, hence `env -u`.
- Dev API: `litwatch serve --port 8766`; the web app's `pnpm dev` (port 5173) proxies `/api` to it.
- Start a dev `serve` with `LITWATCH_STRICT_GATES=1`, as the tests run:

  ```sh
  env -u ANTHROPIC_BASE_URL LITWATCH_STRICT_GATES=1 uv run litwatch serve --port 8766
  ```

  With it, a Database-gate unit opened while another is open on the task, the model gateway called inside
  one, or a Database or Model gate call with no attribution (whom it serves: a work item, a request, or the
  system), raises (`src/litwatch/gates/database.py`). Without it, as in production, the violation is logged as an
  error starting `gate nesting:` or `gate attribution:` and work carries on.
- Commands against a dev `serve`: the commands that call models or run the pipeline (`nightly`, `intake`,
  `digests write`, `fulltext`, `publications`, `ping`, `eval replay`, `eval classify`, `eval briefs`,
  `eval ladders`) write a work item to the database and wait while `serve` runs it, under `serve`'s own
  configuration and models. Start a `serve` on that database first, on a port of your own, with the same
  environment the command gets (`LITWATCH_DATABASE_URL`, `LITWATCH_CONFIG`):

  ```sh
  env -u ANTHROPIC_BASE_URL LITWATCH_STRICT_GATES=1 LITWATCH_API_TOKEN=dev uv run litwatch serve --port 8767 &
  env -u ANTHROPIC_BASE_URL uv run litwatch nightly --no-fetch --limit 5
  ```

  With no `serve` on the database, the command refuses and writes nothing. A command's item gets one attempt: a
  `serve` that stops while it runs it ends it failed, `serve stopped while it ran`, and the command says so, waits
  until a `serve` runs that kind again and submits a fresh item with the same arguments, which goes on from what
  committed (the nightly's steps, the passes, an evaluation's files and records). `--limit` holds per item: a
  resubmission counts afresh. A `serve` killed outright leaves its `workers` row until the next `serve` replaces
  every earlier row (D4: one `serve`), or for up to a lease (3 minutes); its item ends the same way once its lease
  runs out. Ctrl-C asks whether to cancel the item or leave it running: cancelled, `serve` stops it, lets it close
  its run, and the command says how it ended; `litwatch work follow ID` picks one left running back up, its output
  shown from the start and an evaluation's lines written once each. Run again, an evaluation first writes in what
  its last item answered (named beside its output, `<file>.item`), and follows that item to its end first while it
  still runs, then asks what its own arguments still need; `eval replay --resume ID` follows the replay's open item
  first. An evaluation's out paths are read on this host, by the command, not by `serve`. `intake archive --here`
  alone runs in the command, with no item, for a database no `serve` works from (a fresh sandbox's); it calls no
  model.
- The sandbox: `scripts/refresh-sandbox.sh` rebuilds `sandbox` and replaces the old one only once the new one is
  complete. Its two sources:
  - `--production`: a copy of production's database (one read-only `pg_dump` in `litwatch-db`, a few seconds),
    brought to this checkout's schema, with production's open model batches and Deep jobs settled as not followed,
    every integration's cursor at the end of the copied events, its `serve`'s `workers` rows gone and its open
    command items ended, so a sandbox `serve` runs none of production's commands. It has the owner's word, verdicts,
    Deep cards and digests: the source for web checks and data migrations.
  - by default, the migrations plus the papers of a window read from the archive (`--days 14` by default, or
    `--since`/`--until`), as the restart builds one, for pipeline runs from nothing. It only reads the archive.

  From a worktree, pass `--env` with the main checkout's `.env.v1`. Point `LITWATCH_DATABASE_URL` at `sandbox` to
  run against it, and refresh it before a run that writes.
- Stop only what you started: the PID you captured when starting it, or the owner of your port
  (`ss -H -ltnp 'sport = :8766'`) after checking that `/proc/<pid>/cwd` is your checkout. Never kill by a name or
  path pattern: the production container and other worktrees run `litwatch serve` too.
- What Deep did: `litwatch deep trace <paper>` prints the paper's Deep attempts and the latest one's trace and
  review rounds (`--attempt N` another, `--json` everything uncut), and `litwatch deep notes [--since YYYY-MM-DD]` the
  cards' process notes. Both only read, and never migrate. On production they run inside its container,
  which has its database and configuration:

  ```sh
  docker exec litwatch litwatch deep trace 2610.24495
  docker exec litwatch litwatch deep notes --since 2026-10-20
  ```

  The container runs the deployed version: the commands are there once P2 of
  [deep-open-web.md](../plans/deep-open-web.md) is deployed.
- Zotero: the test account's `LITWATCH_ZOTERO_USER_ID` and `ZOTERO_API_KEY` go in `.env.v1`. `.env` carries the
  owner's real library ID and key file, and fills them in whenever `.env.v1` doesn't. Before any real Zotero run,
  check that `.env.v1` sets both, and stop if it doesn't.
