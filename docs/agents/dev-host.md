# The owner's server

This checkout lives on the server that also runs litwatch in production. Production, the v1 deployment and
development share one Docker daemon and one process table.

## What runs here

| Name | What | Where |
| --- | --- | --- |
| containers `litwatch`, `litwatch-deep` | 0.14, in production | |
| containers `litwatch-v1`, `litwatch-v1-db`, `litwatch-deep-v1` | the v1 deployment the owner reads daily | stack `/opt/homelab-config/stacks/litwatch-v1` (its README); API 127.0.0.1:8770, Postgres 127.0.0.1:55433 |
| container `litwatch-v1-dev` | development Postgres | 127.0.0.1:55432; databases `litwatch` (an adopted copy of production), `test` (pytest), `web_demo` (for clicking through the web app) |
| user unit `litwatch-sketches` | the UI sketches | port 8791, serving `docs/specs/v1/sketches/` |

The production and v1 deployments are touched only when the task is about them.

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
- Dev API: `litwatch serve --port 8766`; the web app's `pnpm dev` (port 5173) proxies `/api` to it. Point
  `LITWATCH_DATABASE_URL` at `web_demo` to click through the app without touching dev data.
- Stop only what you started: the PID you captured when starting it, or the owner of your port
  (`ss -H -ltnp 'sport = :8766'`) after checking that `/proc/<pid>/cwd` is your checkout. Never kill by a name or
  path pattern: the production container and other worktrees run `litwatch serve` too.
- Zotero: the test account's `LITWATCH_ZOTERO_USER_ID` and `ZOTERO_API_KEY` go in `.env.v1`. `.env` carries the
  owner's real library ID and key file, and fills them in whenever `.env.v1` doesn't. Before any real Zotero run,
  check that `.env.v1` sets both, and stop if it doesn't.
