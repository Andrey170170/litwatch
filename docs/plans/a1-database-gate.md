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
| **P4** | **Loops and bookkeeping.** The scheduler, jobs worker, dispatch, full-text and Deep loops, and publications move. The model run's journal writes in units of its own instead of on a sibling connection. The session locks move into the gate, and retally's becomes a unit's lock. | The advisory-lock list is empty. One sandbox nightly over a small window with the check strict, as intermediate evidence: intake and digests still run partly outside the gate. Then a clean night (V7). |
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

- **P4: the schedule lock is taken twice on one session today.** `jobs/scheduler.py` holds it for the kind, then
  `jobs/publications.run` takes `job_lock(conn, "publications")` again (`tests/test_publications.py` keeps that).
  `db.held` takes each hold on its own connection, so the second take would be busy: P4 makes one of them the holder
  rather than the gate re-entrant.
- **P4: the run lock's "held elsewhere" needs the caller's own hold.** `db.held_elsewhere("run", mine=…)` leaves out
  only the hold passed in; another task of the same process holding it counts. The nightly passes its `Hold` to its
  full-text step, so that step is not restricted by its own nightly.
- **P4 or P6: the migration lock** is still taken in `store/db.py` (on the advisory-lock list); it moves behind the
  gate before that list is empty.
- **P4 and P5: P2's boundaries still build the bridge.** Where a caller holds a raw connection, P2 opens its units
  on `DatabaseGate.over(conn)`: the nightly's steps, the jobs worker, re-runs, and the full-text and Deep loops (P4),
  intake's collectors and the digests (P5). Each becomes the caller's `db`. P3 removed the API's owner actions'
  bridges and the CLI's `taxonomy import` and `library sync`. `fulltext --deep`'s is not the CLI's: it is inside
  `fulltext.deep._work`, which the Deep loop shares, and goes with it in P4. `feedback.calibrate.answer`,
  `intake.batch.store`, `word.sync` and `word.settle_all` take `db` already.
- **P4 to P6: `db.lend(name)`, P3's transitional means.** An entry calling a Service that still takes a raw
  connection borrows one outside any unit: a connection of its own, as `held`'s are, opened for the loan and closed
  at its end, in autocommit and outside the pool, so a loan held across a network call (arXiv's waits, up to 45 s)
  or one whose Service opens units of its own (`grade_now`) takes no pool slot. The gate refuses to lend while a
  unit is open on the task, as it refuses a unit. It goes in P6, when nothing is left to lend to. Each place, with
  the stage
  whose Service move takes it away:
  - P4: `api/reading.py` `set_publication` and `bibtex_selection` (`jobs.publications`); `api/settings.py`
    `grade_now` (`jobs.context.model_run`, its run record and journal); `api/jobs.py` `replay_offer` and
    `run_replay` (`jobs.replays.offer` and `make`, in a transaction of their own on the lent connection);
    `cli.py` `nightly`, `publications` and `fulltext` (with `--deep` too).
  - P5: `api/deps.py` `resolve_paper` (`feedback.misses.resolve`, for `report_miss`, `process` and
    `request_card`); `api/settings.py` `draw` and `more` (`feedback.calibrate`); `cli.py` `intake` (all three
    forms, with the run lock taken on the lent connection), `digests write`, `eval replay`, `eval ladders` and
    `eval ladders-report`.
  - P4 or P6, with the migration lock: `cli.py` `_migrate` (`store.migrate`), which `serve` and every migrating
    command run.
- **P4 and P5: Service reads that take a read unit already.** Plain reads in Service modules are passed the
  entry's read unit, their bodies unchanged, though their signatures still say a connection:
  `feedback.calibrate.sessions`, `session`, `precision` and `matters_no_topic` (P5), and `jobs.reruns.scope_papers`,
  `per_paper` and `unpriced` (P4). The stage that moves each types it.
- **P4: the app's loops still take `app.state.dsn`** and open their own connections; the routes, MCP and the
  monitor use `app.state.db` (a pool of at most 10). `serve` opens one gate for the process before it migrates,
  gives it to the app and so to the monitor, and closes it after both servers return; an app given no gate opens
  its own in its lifespan, first, and closes it last. Tests that call the app without its lifespan open its gate
  through `tests.conftest.AppClient`.
- **P5: two SQL calls are left in the entries** (`tests/test_sql_home.py`): `api/integrations.py`
  `search_outside`'s look-up of the papers held, and `api/reading.py` `_bibtex`'s read of their citations. Both
  run in read units now.
- **P3 to P5: a block inside a P2 unit must not use the raw connection.** On the bridge the unit and the connection
  are one session, so a statement on the connection inside the unit works by accident; on the pool it would run
  outside the unit. P2's units pass the unit everywhere, and the stage that drops a bridge checks its blocks again.
- **P6: `store.db.transaction(conn)`** stands in for `conn.transaction()` in the store functions reached both with
  a unit and with a connection (`taxonomy.load`, `save` and `set_order`, `papers.upsert` and `correct`,
  `digests.record`, `integrations.put_card`): a transaction on a connection, nothing on a unit. `taxonomy.lock`
  and `lock_shared` likewise only check a unit's hold. Both go when no caller passes a connection.
- **P4: `fulltext.deep._service_failed`** and `store.fulltext.take_back` and `retry_failed` still open their own
  transactions on a connection; no settle runs in them, so P2 left them.
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
