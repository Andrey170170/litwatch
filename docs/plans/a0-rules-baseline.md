# A0: rules and baseline

Approved by the owner on 2026-11-10. The first milestone of [`docs/specs/architecture.md`](../specs/architecture.md);
its items are listed under "A0" in [`docs/backlog.md`](../backlog.md). Where this plan and the spec differ, the spec
wins and this plan is corrected.

## Goal and approach

Put the spec's role rules into CI, with today's violations listed so they can only shrink; find what production no
longer uses and remove it, so no later milestone migrates dead code; land the 2026-10-21 review's small fixes. No
behaviour changes.

Three stages, in order. The review's small fixes run alongside all three, one PR each.

## Choices made without asking

- **Roles are checked at the package level for now.** Today's packages map to roles: `core` holds Records and
  Policies, `store` Repositories, `llm` is the Model gate, `api` and `cli` are Entries, and every other package
  counts as Services (mixed today). Finer, per-module rules come as each milestone gives its modules role names (D1).
- **Four rules in A0:**
  - `core` imports nothing of litwatch outside `core`;
  - `store` imports only `core` and itself;
  - a gate (`llm` today) imports no Service package and no Entry;
  - nothing imports an Entry (`cli` may import `api`, both being Entries).

  Plus: no import cycle between Service packages.
- **Tools.** `import-linter` as a dev dependency, its contracts in `pyproject.toml`, run in CI beside ruff
  (`uv run lint-imports`). The cycle rule is one pytest over `grimp`'s import graph (the library import-linter is
  built on), since import-linter's contracts state layers and forbidden imports rather than cycles among siblings.
- **Today's violations are listed and may only shrink,** as `tests/test_sql_home.py` does: import-linter's
  `ignore_imports` for the contracts, an allow-list in the cycle test. A listed violation that no longer occurs
  fails the check too (import-linter reports unmatched ignores as errors; the cycle test asserts each listed cycle
  still exists), so the lists can't go stale. Known on `b48d76f`: `store → llm` (`store/calls.py` imports
  `llm.types.Usage`); the cycles `intake ↔ feedback`, `fulltext ↔ jobs` (the import inside `fulltext/deep.py`
  counts) and `jobs → quality → jobs`. The PR lists the full set the tools report.
- **The analysis is read-only,** delegated to GPT-6.1 Sol: production's non-secret configuration, the call journal by
  role, the evaluation tables' row counts and last writes, routes' callers in `web/`, MCP and the CLI, config options
  against what `config.py` defines and production sets. Production keeps no API access log, so a route only the
  owner's own scripts might call is put to the owner, not guessed.
- **The review's small fixes ship one PR each,** in parallel with the stages, each in its own worktree:
  - `.dockerignore`: add `.env.v1`, `deep/node_modules`, nested `.venv`s;
  - deep out of the event dispatcher (its `wants()` is always false), keeping its `aclose()`. The Deep read's P3
    (#146) has merged, so this no longer collides with it;
  - the dead pre-OAI arXiv transport: `_ArxivSession`, `_ArxivClient`, `search`, `window_query`, `fetch_window` in
    `intake/search.py`, and `oai.fetch_versions`; `fetch_by_ids`, `id_from_url` and `NoAnswerError` stay;
  - `usePaperBusy` (`web/src/api/mutations.ts`) filtered by paper id inside its subscription;
  - queries passing React Query's `signal` (`web/src/api/queries.ts`);
  - `httpx2` declared in `pyproject.toml`;
  - the web scaffold leftovers: `class-variance-authority`, `clsx`, `tailwind-merge`, `tw-animate-css`;
    `WEIGHT_NAME`, `weightFloor`, `clearSnapshots`; the `.oldrev` style;
  - stale guidance: `docs/agents/verification.md`'s milestone wording, the v1 spec README's "being built", N9's MIT
    licence and its "`evals/` leaves the repository".

  All were checked still needed on `b48d76f`. `/summary` and Calibrate's double read go with A1.
- **AGENTS.md's "Rules the code keeps"** gains one line pointing at the checks and the spec.

## Decisions

- **D1. When modules get their role names: each milestone renames its own area.** A1 renames `store` and
  settling, A2 the workers, and so on; the checks tighten as each area is renamed. Chosen over renaming everything
  in A0, which would touch most files while the Deep read is being built and rename modules a later milestone
  rewrites anyway. The cost: for a while most of the tree is checked only at the package level.
- **D2. What production no longer uses: A0 removes it.** After the owner marks the keep/drop list, A0 removes the
  drops, one PR per area, so no later milestone migrates them. A drop inside an area a later milestone rewrites
  anyway is left to that milestone. Chosen over listing only, which would carry dead code through A1 and A2.

## Risks

- **R1. The violation lists become a place to park new violations.** The checks fail on an unlisted violation and
  on a stale entry, and the lists sit in one reviewed place, so a PR that adds to them stands out.
- **R2. Something rare looks unused** (a yearly CLI command, an option set only on another install, a route the
  owner's scripts call). The analysis marks each item with its evidence and a confidence, and nothing is removed
  without the owner's word on it. Before a config option is removed, production's `litwatch.toml` is checked for
  it, so the service still starts.
- **R3. Collisions with the Deep read,** built on `main` at the same time (P4 and P5 remain). A0 changes no pipeline
  code except removals; a removal touching `fulltext/deep.py`, `integrations/deep.py` beyond the dispatcher fix, or
  `deep/` waits for the Deep read's P5.

## Stages

| | Stage | Shows before the next starts |
|---|---|---|
| P1 | The role checks: import-linter contracts, the cycle test, today's violations listed, CI and AGENTS.md updated. One PR. | V1, V2 |
| P2 | What production still uses: the read-only analysis, written to `docs/notes/still-used.md` as a keep/drop list with evidence; the owner marks each item. | V3, and the owner's marks |
| P3 | The drops the owner approved, one PR per area (config, roles, tables, routes, commands). | V4 per PR |

A0 is done when P1–P3 and the small fixes are merged; then its items leave the backlog.

## Verification

| | Claim | Evidence |
|---|---|---|
| V1 | The role rules hold, and a new violation fails CI | `lint-imports` and the cycle test pass in CI; locally, an added `core → store` import and an added Service cycle each fail with a readable message (output in the PR) |
| V2 | The violation lists are exactly today's | Removing a listed entry fails the check; removing a listed import makes its stale entry fail |
| V3 | Every keep/drop item rests on evidence | Each item in the note cites its production query, the callers searched, and a confidence; the owner's mark on each |
| V4 | A removal breaks nothing in use | CI green; for config, production's `litwatch.toml` checked free of the removed keys; for a table, the migration on a fresh schema and on a sandbox copied from production |
| V5 | Each small fix does what it says | `.dockerignore`: build context size before and after; `usePaperBusy`: a vitest counting renders; `signal`: a test that an aborted query stops its fetch; `httpx2`: `uv sync` in a clean environment; scaffold: web build and type check; the rest by CI |

## Scope

- **In:** package-level role checks and the cycle test; the still-used analysis and the approved drops; the review's
  small fixes; AGENTS.md's pointer.
- **Out:** bypass checks for raw HTTP, threads and advisory locks (each with its gate, A1 and A5); the runtime
  nesting check (A1) and the attribution check (A2); module renames (D1, each milestone); `/summary` and Calibrate's
  double read (A1).
