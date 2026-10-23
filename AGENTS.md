# litwatch

litwatch reads every new arXiv paper in its categories, sorts and judges each against the owner's topics, and serves
the results through its own web app. It is single-user and self-hosted. v1, on `main`, runs in
production since 2026-10-17, in place of release 0.14.

## Where things are

- `GLOSSARY.md`: the domain terms. Code, docs and generated prose use them as defined there.
- `docs/specs/v1/`: what v1 does and why, one file per subject, indexed by its `README.md`. `milestones.md` there
  says what is built and what comes next; `DESIGN.md` there is the web app.
- `docs/backlog.md`: everything specified or agreed but not built yet, in the order it should be picked up.
- `docs/plans/`: approved build plans; `v1-build.md` is the order of work for v1, its PRs and their evidence.
- `docs/install.md`: installing a fresh litwatch with the stack in `deploy/`; keep it in step with `deploy/`.
- `docs/agents/workflow.md`: how changes ship here. `docs/agents/verification.md`: how a change is shown to work.
- `src/litwatch/`: the Python service (CLI, nightly jobs, API). Each package's `__init__.py` docstring states its job.
- `web/`: the web app (React, Vite, pnpm). `pnpm build` writes into `src/litwatch/api/web/`, which the service serves.
- `deep/`: the deep-card service (Node, TypeScript), a separate package with its own image.
- `archive/`: the archive (Python, its own package and tests); the store of arXiv papers litwatch ships with.
- `evals/`: evaluation reports, their configs and the labelled sets.

## Rules the code keeps

- `core` is pure: domain types and rules, no I/O, no imports from the rest of litwatch.
- `store` is the only package with SQL; its functions take a connection and return `core` types. Schema changes are
  migrations in `store/migrations/`.
- Pipeline code names an `llm` role, never a model; models are configuration (`litwatch.toml`).
- Evaluation runs are dry runs: they write to the evaluation tables only, never to verdicts or the outbox.
- A pipeline step writes its event to the outbox in the same transaction as its data. Integrations react to events
  and are off unless configured.
- A write that can move a paper is a change (`feedback.word.change`): it takes the locks, taxonomy then papers in
  id order, checks and writes under them, and settles each paper once before commit. Taxonomy edits keep their own
  flow, with the taxonomy lock taken exclusively.

## Checks

CI (`.github/workflows/ci.yaml`) runs all of these; run the ones your change touches before committing.

- `uv run ruff check src tests`, `uv run ruff format --check src tests`
- `uv run pytest --strict-markers`. Tests that need PostgreSQL create and drop their own schema in
  `TEST_DATABASE_URL`, which must be a disposable database, and skip without it. Run one suite at a time per database
  cluster: the outbox reads the cluster-wide snapshot, so another run's open transactions hide fresh events.
- In `deep/`: `npm run typecheck`, `npm test`. In `web/`: `pnpm typecheck`, `pnpm test`. In `archive/`:
  `.venv/bin/python -m unittest discover -s tests` (setup in its README).

After an API change, regenerate the client types: `uv run litwatch openapi web/openapi.json`, then `pnpm generate` in
`web/`.

## On the owner's server

`.env` in this checkout points at the production database, and every `litwatch` command loads it. Before running
litwatch against any database, or starting or stopping a server, read `docs/agents/dev-host.md`.
