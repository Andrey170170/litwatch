# A1: Database gate

Approved by the owner on 2026-11-17. The build plan for milestone A1 of
[`docs/specs/architecture.md`](../specs/architecture.md), shaped in
[`docs/specs/architecture/a1-database-gate.md`](../specs/architecture/a1-database-gate.md); its folded items are
listed under "A1" in [`docs/backlog.md`](../backlog.md). Where this plan and the spec differ, the spec wins and this
plan is corrected.

## Goal and approach

One pool per process. Every database access goes through a typed unit from the Database gate, and every lock is
named, ordered and owned by it. Nesting is strict and checked at runtime. Settling a paper becomes cheap and runs
once per paper. Nothing outside the gate holds a connection.

The work is in seven stages:
- **P1** builds the gate beside today's connections.
- **P2 to P5** move the code onto it area by area. Settling and `change` go first, as the heart of A1.
- **P6** tightens the checks until every list is empty and adds `ty`.
- **P7** builds the folded backlog items on the finished gate.

Each stage runs in production before the next starts.

## Choices made without asking

- **Where the gate lives.** `src/litwatch/gates/database.py` holds `DatabaseGate`, `ReadUnit` and `WriteUnit`, as
  the spec's layout puts gates under `gates/`. The pool is `psycopg_pool`'s `AsyncConnectionPool` (the
  `psycopg[pool]` extra), not a hand-made pool.
- **A unit speaks the connection's language:** `execute`, `executemany`, `cursor`, `copy`. A Repository's body
  doesn't change, only its signature. The `transaction()` calls inside `store` (11 of them) go, because the unit is
  the transaction.
- **A read unit runs `REPEATABLE READ READ ONLY`.** Postgres itself rejects a write in it. A read unit takes no
  advisory lock: `taxonomy.load` stops taking the shared taxonomy lock inside a read unit, because the snapshot is
  enough.
- **The locks are in one registry** in the gate, each with its name, key, scope (unit or session) and position in
  the order. A unit's own locks are transaction locks.
  - The four session locks on the held-across-calls list (run, Deep worker, dispatch, schedule-kind) each get a
    dedicated connection that the gate opens outside the pool, so a long hold never takes a unit's connection.
  - Retally's lock becomes a transaction lock of its write unit.
  - `held_elsewhere` (is the nightly running?) moves into the gate with the run lock.
- **Strict or lenient by environment.** The gate raises on a nesting violation when `LITWATCH_STRICT_GATES=1`.
  `tests/conftest.py` sets it, and `docs/agents/dev-host.md` sets it for a dev `serve`. Without it, the gate logs the
  violation at error level and carries on. The check is a context variable naming the open unit. A task started
  inside a unit inherits it, which is right: that task's units and model calls run while the parent's locks are
  held.
- **A unit open longer than 5 s is logged** as a warning, with its name. This is not A4's telemetry, only enough to
  find a long unit before A3 depends on short ones. A long unit also holds back the outbox, which waits on the
  cluster's oldest snapshot.
- **A bridge while code moves.** `DatabaseGate.over(conn)` is a gate whose units all run on one given connection,
  with no pool, so code not yet migrated can call a migrated Service. It refuses a connection already inside a
  transaction, so nesting stays strict. It goes in P6, unless tests still need it as a helper. It cannot serve a
  caller that already holds a transaction around a settle (`request_card`, `jobs.process`, intake's page write, the
  taxonomy save, a job's end): those callers move to units in P2, with `change`.
- **A process's identity is its gate's presence session** (decided by Andrei on 2026-11-18, while P4 was built).
  Run records and job claims name the session that runs them (`backend_pid`, `backend_start`), and `ALIVE` and
  `store.jobs.take_back` judge them by it; on the pool, no one connection lasts as long as a run or a claim. Each gate
  holds one connection of its own for the process's life, outside the pool: opened with the gate, closed with it,
  and opened again when it dropped. Its `(pid, backend_start)` is the process's `Identity` (`db.identity()`), which
  `runs.start` and the job SQL (`claim`, `progress`, `wait`, `finish`) take as a parameter; `ALIVE` and `take_back`
  are unchanged. A presence opened again has a new identity, so the claims made under the old one are taken back,
  and the worker then closes its open runs and claims afresh. A bridged gate's presence is its one connection.
  Transitional: A2's Work gate replaces it with leases and heartbeats (`docs/backlog.md`, A2, "Leases replace the
  presence session"). Rejected: a dedicated connection per run or claim (one per open job, for as long as a re-run
  waits on its text), keeping the runs and the worker on lent connections (P4's loops would stay off the pool), and
  leases now (A2's design, ahead of it).
- **Settling takes its inputs explicitly:** `settle_many(unit, ids, cause, rules)`.
  - It reads the taxonomy once, when settling starts, so a unit that edits the taxonomy settles against its edit.
  - It reads labels, reactions, weights and current verdicts (not their history) in one query each for the whole
    set, in chunks of 500 papers.
  - The per-paper rule stays in Python.
  - `rules` carries `automatic_deep`, which was a module global the CLI set when it loaded the configuration. It
    is `word.Rules` now: a boundary that has the configuration reads it with `word.rules(config)`, and one that
    has none takes `rules` from its caller (the API through the `deps.Rules` dependency).
  - A taxonomy edit collects every paper its changed topics reach and settles each once.
- **Store parameters are renamed** from `conn` to `unit` in P6, by script, once every caller passes units. Doing it
  in one go keeps the earlier PRs' diffs readable.
- **Two new search checks** start from today's list, which may only shrink: no advisory-lock SQL outside the gate,
  and no `AsyncConnection` outside the gate and `store`. Both lists are empty when A1 is done.
  `tests/test_sql_home.py` exempts `gates/database.py` as it exempts `store/`, in P1: the gate's own SQL (locks,
  transaction settings) is resource management, and application queries stay in `store`.
- **`ty` runs on `src` and `tests`** in CI, beside ruff, from P6 (D2). Its baseline is committed at the repository's
  root. When a fix clears a baselined diagnostic, the same PR regenerates the baseline, so it shrinks.
- **Folded items come after P6,** one PR each and in parallel among themselves, built on the finished gate. The SQL
  outside `store`, the multi-paper `change` tests and `store`'s import violations are not in P7: they belong to the
  gate stages themselves.
- **Each stage is a hard-class PR** with drive's review loop. Fable or Opus implements, never Haiku or Luna, and Sol
  reviews.

## Structure

```text
Entry / loop ──db──▶ Service ──db.write(papers=…)──▶ WriteUnit ──unit──▶ helpers, Repositories
Entry (plain read) ──db.read()──▶ ReadUnit ──▶ Repositories
nightly, Deep worker, dispatch, scheduler ──db.held(name)──▶ session lock on its own connection
                                                             (listed; emptied by A2 and A3)
```

A function that takes `db` is a boundary and opens its own units. A function that takes a unit runs inside one and
never commits.

## Decisions

- **D1. A Service reaches the gate by being passed it, as `db`.** It replaces today's `conn` parameter at the
  boundaries, so a signature tells a boundary (`db`) from an inside step (a unit). There is no hidden state, and
  tests hand in their own gate. The cost is passing `db` through the layers, plus the bridge while code moves.
  Chosen over one gate per process fetched where needed, which hides the dependency and gives up part of what strict
  nesting is for.
- **D2. `ty` in CI from P6, with a baseline.** It is Astral's checker, alongside ruff and uv (`uv run ty check`), with
  its version pinned in the dev dependencies. Today's diagnostics go into `ty-baseline.json` through
  `ty check --update-baseline`, and new code adds none. The typed units then reject a write in a read unit, as the
  spec says. Chosen over basedpyright, and over no type checker at all (Postgres's read-only transaction would catch
  the mistake only where a test runs that path).
- **D3. Every stage deploys before the next starts,** as the spec says. P1 runs nothing, so it goes out with P2:
  five deploys for the gate and one for the folded items. Each deploy is the owner's command, which the agent
  prepares. Chosen over three checkpoint deploys, which would make a bad night harder to trace to one stage.

## Risks

- **R1. Settling gives different results after the rework.** Batched reads and "once per paper" change how the
  inputs arrive, and a subtle difference would quietly regrade papers. Handled by an equivalence check on a sandbox
  copied from production (V3).
- **R2. The pool runs dry, or a lock order deadlocks,** under the nightly's concurrency, which tests don't reach.
  Pool waits have a timeout that raises with the unit's name, and session locks don't use the pool. The `change`
  tests cover overlapping papers in reverse order, and the first night after each deploy is checked (V7).
- **R3. In production, a violation is logged and missed.** Lenient mode keeps the night running but hides the
  violation. After each deploy, the logs are searched for the check's messages and the slow-unit warnings, and
  whatever turns up is fixed before the next stage (V7).
- **R4. The mechanical migration collides with other work.** P3 to P6 touch most modules and about 2,600 test
  lines. They run one at a time, with no other backend PR open meanwhile. Product work waits, or stays in `web/`.
- **R5. `ty`'s baseline is in preview.** A release could change the baseline's format or its diagnostics and fail
  CI unexpectedly. `ty` is pinned, and an upgrade is its own small PR that regenerates the baseline.

## Stages

| | Stage | Shows before the next starts |
|---|---|---|
| **P1** | **The gate.** Pool; typed units; the lock registry and its order; the session locks on their list; the nesting check (a unit inside a unit, and the model gateway's `complete`, `decide` and batch submit); the slow-unit warning; the `over(conn)` bridge; the two new search checks with today's lists. Nothing uses it yet. | The gate's tests pass (V1, V2), and CI is green with the new checks. |
| **P2** | **Settling and `change`.** `change` becomes a Service over a write unit, with `settle_many` doing batched reads. The steps' `record(unit, …)` move with it, and so does every write that settles inside a transaction of its own: those transactions become units now, and the rest of their modules move in their later stage. A taxonomy edit settles each paper once. `automatic_deep` becomes an input. Plus the multi-paper `change` tests. | Equivalence on the sandbox (V3), the timing before and after (V4), the change tests (V2). Deployed with P1, then a clean night (V7). |
| **P3** | **Entries.** The API's `Conn` becomes `db`, with plain reads in read units. The CLI builds one gate per process. MCP and the monitor app move too. | The API tests and the stack smoke test pass, then a clean night (V7). |
| **P4** | **Loops and bookkeeping.** The scheduler, jobs worker, dispatch, full-text and Deep loops, and publications move. The model run's journal writes in units of its own instead of on a sibling connection. The session locks move into the gate, and retally's becomes a unit's lock. | The advisory-lock list holds one entry, the taxonomy lock's raw-SQL branch (`store/taxonomy.py`), until P6. One sandbox nightly over a small window with the check strict, as intermediate evidence: intake and digests still run partly outside the gate. Then a clean night (V7). |
| **P5** | **The remaining Services.** Intake, digests, Calibrate, misses, quality, replay. The SQL outside `store` moves into it, `events.emit` included, which ends `store`'s imports of `events` and `llm`. | `tests/test_sql_home.py`'s list is empty, `store`'s import-linter ignores are gone, then a clean night. |
| **P6** | **Tightening.** `store.connect` and `sibling` go, the bridge stays only as a test helper, and store parameters are renamed `unit`. The connection list is empty. `ty` runs in CI with its baseline (D2). | Every A1 check list is empty and CI is green; the sandbox nightly on the whole gate (V6); then a clean night. |
| **P7** | **Folded items,** one PR each, in parallel: `/summary` with the sidebar's count and today in the schedule's zone; Calibrate reading its pool once; the four read gaps; intake leaving unchanged rows alone; the drops from `notes/still-used.md`. | Each shown by its kind (V8), then deployed together. |

## Verification

| | Claim | Evidence |
|---|---|---|
| **V1** | A read unit reads one snapshot and refuses writes. A write unit commits, or rolls back on error. Opening a unit inside a unit, or calling the model gateway inside one, raises when strict and logs when lenient. | pytest on real Postgres (`tests/test_database_gate.py`), one test per sentence. |
| **V2** | Locks are taken in the order the gate sets. A lock asked for late raises, except papers under the exclusive taxonomy lock. Two changes on overlapping papers, named in reverse order on two connections, both finish. A later paper's failure rolls the whole change back. | pytest: two concurrent units under a timeout that fails on a deadlock. |
| **V3** | Settling decides exactly as before. | On a sandbox copied from production, run `litwatch library sync` at the base commit and dump the settled tables (effective grades, rarity, library, queued passes). Do the same at P2's head, diff the two dumps and find them identical. |
| **V4** | Settling is cheap: the 6,777-paper Field re-settles in far fewer statements and less time. | On the same sandbox, the Field's `settle_topic` is timed and its statements counted (by a counting wrapper on the unit), before and after. The numbers go in the PR and in this plan's evidence. |
| **V5** | Nothing outside the gate holds a connection or takes an advisory lock, and no application SQL is left outside `store` (the gate keeps only its own lock and transaction SQL). | In CI: the search checks and `test_sql_home.py`, their lists empty at P6; `lint-imports` with no ignores for `store`; `ty check` against its baseline. The PR shows `ty` flagging a writing Repository given a `ReadUnit`. |
| **V6** | The pipeline runs end to end on the gate. | At P6, when nothing bypasses the gate any more, the nightly runs on the sandbox over one small window with `LITWATCH_STRICT_GATES=1`. Its run record is compared with a run of the same window before A1, and no check may raise. The same run after P4 is intermediate evidence only. At most $5 a run. |
| **V7** | Production holds after each deploy. | The next night's run record. The service logs searched for the check's errors, slow-unit warnings and pool timeouts. A read-only look for blocked locks during the run. Each finding is fixed before the next stage. |
| **V8** | Each folded item does what its backlog entry says. | By its kind in `docs/agents/verification.md`. API tests and the regenerated client for API changes. The sidebar count and today's date walked in the web app on the sandbox at both widths. Intake's untouched rows counted on a re-run of the overlap. |

## Scope

**In:** the spec, the items folded into A1 in the backlog, and `ty` in CI with its baseline.

**Out,** each to the milestone that owns it:
- the taxonomy edit committing first, and the run lock: A3;
- the loops becoming Workers, and attribution: A2;
- slow units and lock waits recorded on work records: A4;
- the Fetch and Compute gates: A5;
- ratings on the generated text: A3.

## Notes for later stages

Found while building, owned by the stage named.

- **P6: what P5 left, all of it reached from the tests only.** P5 moved the last Services onto the gate: nothing in
  `src` outside `gates/` and `store/` names a connection (the connection list is empty), no SQL is left outside
  `store` (`tests/test_sql_home.py`'s list is empty), `store` imports neither `events` nor `llm` (no contract keeps
  an `ignore_imports`), and `db.lend` went with its last caller. No block inside a unit uses a raw connection: there
  is none left in `src`. What remains for P6, by name:
  - `store.connect` and `store.sibling` (`store/db.py`, exported by `store/__init__.py`), with no caller in `src`:
    the tests' fixtures, and the tests that stage a second session (`test_jobs.py`, `test_dispatch.py`,
    `test_reading.py`, `test_word.py`, `test_grading.py`, `test_batches.py`, `test_api_integrations.py`).
  - `DatabaseGate.over(conn)` and its bridged branches (`_connection`, `identity`, `held_elsewhere`), with no caller
    in `src`: `tests/settling.py` and about fifteen test modules reach the Services through it. It stays as a
    test helper, or gives way to the pooled `db` fixture.
  - The taxonomy lock's raw branch (`store/taxonomy.py`'s `lock` and `lock_shared`, the advisory-lock list's one
    entry in `tests/test_database_home.py`), and `store.db.transaction(conn)` in `taxonomy.load`, `save` and
    `set_order`, `papers.upsert` and `correct`, `digests.record` and `integrations.put_card`: only tests pass them a
    connection now, about 65 of them seeding with `taxonomy_store.save(conn, …)`.
  - Store parameters renamed `unit`: a store function still says `psycopg.AsyncConnection` though every caller in
    `src` passes a unit (`store.outbox.emit` says `WriteUnit` already). Tests pass raw connections to functions
    typed for units (`outbox.emit`; `feedback.calibrate`'s `session`, `sessions`, `precision` and
    `matters_no_topic`; `quality.replay`'s `prepare`, `resume`, `estimate`, `misses` and `start`), which `ty` flags
    once it runs on `tests`.
- **P4 found no need for `held_elsewhere(mine=…)` in the code.** The nightly passes its run to its full-text step,
  and `fulltext.work` with a run never asks whether the run lock is held elsewhere. The parameter stays for a caller
  that holds the lock without a run.
- **A2: the presence session's reconnect.** `db.identity()` checks the presence with a `SELECT 1` each time it is
  asked (a run's start, each worker step) and opens it again when it dropped; between asks a drop goes unseen until
  the next. Leases replace it (`docs/backlog.md`, A2).
- **A2: work is not fenced when its ownership is lost.** Ownership lives on side sessions (the session locks' and
  the presence) that the pooled work never watches, so a block goes on after its lock's or the presence's session
  dropped, and a run whose last close fails stays alive. The cases and A2's acceptance are in `docs/backlog.md`, A2,
  "Work fenced when its ownership is lost".
- **P3: an owner action that may only answer is not a `change`.** `api/jobs.process` and
  `api/integrations.request_card` return early when the work is already under way, and a `change` would settle
  the paper on that path too. They open `db.write(..., taxonomy="shared", papers=[id])` themselves and call
  `settle_many` after they wrote, as `intake.batch.store` does for the papers a page rewrote. The worker's `_end`
  and `api/jobs.act` open the same unit for the papers' locks alone and settle nothing, as before.
- **V4: `word.settle_topic` has no caller in the code** since a taxonomy edit collects its papers itself
  (`save_taxonomy`). It is kept for V4's measurement and for a one-topic re-settle by hand.

## Evidence

Filled in as stages land.

- **P2** (V3, V4), on a copy of production taken 2026-11-17 (22,549 papers), restored before each run:
  - V3: `litwatch library sync` at the base (7965b778) and at P2 both settle 22,526 papers with 0 grades changed,
    0 saved and 0 unsaved. Effective grades, rarities, library, full-text passes, jobs and outbox counts dumped
    after each run are identical. The sync took about 2m51s at the base and 11.8 s at P2.
  - V4: the Field is `language-models`, which `settle_topic` finds 8,547 papers for today (6,777 was the count in
    the wait note). Its `settle_topic`, under the exclusive taxonomy lock and rolled back, went from 162,275
    statements and 69.7–70.5 s at the base to 203 statements and 3.3–3.5 s at P2, over three runs each.
- **P1+P2, V7:** deployed 2026-11-17 17:40 MST as `litwatch:1.0.0-dev-0236ac41`. The night of 2026-11-18 (run 113)
  finished in 42 min with no error: 643 classified, 695 graded, 44 Briefs, 5 digests, $3.71, in line with the week
  (40 min to 1 h 6 min on nights with new papers). Its events per type kept the previous night's proportions (one
  `grade.changed` per verdict recorded, 14 saved, 1 unsaved). From the deploy to 08:30 the log has no error, no
  traceback, no `gate nesting:` line and no slow unit; its only warnings are four model refusals. Every `/status`
  component is healthy. Blocked locks were not sampled during this run (`log_lock_waits` is off); Postgres counts
  no deadlock since 2026-10-22. From P3 on, a read-only `pg_locks` sampler watches each checked night.
- **P4, sandbox nightly (intermediate evidence):** a sandbox built from the archive's papers of 2026-11-17 (319) with
  production's taxonomy, no integrations but the archive, `LITWATCH_STRICT_GATES=1`, `litwatch nightly --no-fetch
  --limit 25`: exit 0 in 54 s, $0.08; 25 classified, 25 analysed, 23 graded, 4 full-text passes reconciled, 1 digest
  written and 4 with nothing new. The log has no warning, error, `gate nesting`, slow unit or pool timeout.
- **P3, V7:** deployed 2026-11-18 10:55 MST as `litwatch:1.0.0-dev-028edc42`. The night of 2026-11-19 (run 115)
  finished in 42 min with no error: 614 classified, 662 graded, 38 Briefs, 5 digests, $2.93, in line with run 113.
  Its events per type kept run 113's proportions (660 `grade.changed` for 662 verdicts, 7 saved, 2 unsaved). One
  analysis and one Brief failed on the model's output (a Gist of 52 words, a Brief of 28), to be retried. From the
  deploy to 08:30 the log has only info lines: no error, traceback, `gate nesting:`, slow unit or pool timeout. The
  `pg_locks` sampler, every 2 s from 01:55 to 04:30, saw no backend waiting on a lock. Every `/status` component
  is healthy.
