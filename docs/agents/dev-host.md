# The owner's server

This checkout lives on the server that also runs litwatch in production. Production, the v1 deployment and
development share one Docker daemon and one process table.

## What runs here

| Name | What | Where |
| --- | --- | --- |
| containers `litwatch`, `litwatch-deep` | 0.14, in production | |
| containers `litwatch-v1`, `litwatch-v1-db`, `litwatch-deep-v1` | the first v1's deployment, **stopped** since 2026-10-14 (not removed; its database dumped to `~/backups/litwatch-v1-first-build-2026-10-14.dump`) | stack `/opt/homelab-config/stacks/litwatch-v1` (its README) |
| container `litwatch-v1-dev` | development Postgres | 127.0.0.1:55432; databases `litwatch` (an adopted copy of production), `test` (pytest), `sandbox` (end-to-end runs) |
| user unit `litwatch-sketches` | the UI sketches | port 8791, serving `docs/specs/v1/sketches/` |

Production and the stopped v1 deployment are touched only when the task is about them.

## Running litwatch in development

```sh
set -a; . ./.env.v1; set +a
env -u ANTHROPIC_BASE_URL uv run litwatch …
```

- `.env` in this checkout points at the **production** database, and every `litwatch` command loads it for any
  variable not already set. The gitignored `.env.v1` sets `LITWATCH_DATABASE_URL` (dev `litwatch`),
  `TEST_DATABASE_URL` (dev `test`) and `LITWATCH_API_TOKEN`; source it first so `.env` only fills in the API keys.
- The agent shell exports `ANTHROPIC_BASE_URL` for the agent's own proxy. The Anthropic SDK picks it up and every
  litwatch call fails with 401, hence `env -u`.
- Dev API: `litwatch serve --port 8766`; the web app's `pnpm dev` (port 5173) proxies `/api` to it.
- The sandbox: `scripts/refresh-sandbox.sh` builds `sandbox` fresh, the migrations plus the papers of a window read
  from the archive (`--days 14` by default, or `--since`/`--until`), and replaces the old one only once the new one
  is complete. It only reads the archive, at `LITWATCH_ARCHIVE_URL` with `LITWATCH_ARCHIVE_TOKEN`; from a worktree,
  pass `--env` with the main checkout's `.env.v1`. Until it first runs, `sandbox` is the last copy of the first
  v1's deployment, whose containers stopped on 2026-10-14. Point `LITWATCH_DATABASE_URL` at `sandbox` to run
  against it, and refresh it before a run that writes.
- Stop only what you started: the PID you captured when starting it, or the owner of your port
  (`ss -H -ltnp 'sport = :8766'`) after checking that `/proc/<pid>/cwd` is your checkout. Never kill by a name or
  path pattern: the production container and other worktrees run `litwatch serve` too.
- Zotero: the test account's `LITWATCH_ZOTERO_USER_ID` and `ZOTERO_API_KEY` go in `.env.v1`. `.env` carries the
  owner's real library ID and key file, and fills them in whenever `.env.v1` doesn't. Before any real Zotero run,
  check that `.env.v1` sets both, and stop if it doesn't.
