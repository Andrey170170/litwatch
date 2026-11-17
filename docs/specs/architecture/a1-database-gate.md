# A1. Database gate

Status (2026-11-17): **planned, not built**; the build plan is
[`plans/a1-database-gate.md`](../../plans/a1-database-gate.md). This is milestone A1 of
[the architecture](../architecture.md): the Database gate, its units and its locks, and settling a paper made cheap.
Its input is the measurement in [`notes/where-work-waits.md`](../../notes/where-work-waits.md).

## Why

**A1 fixes no wait the owner feels today.** Over 26 hours with a nightly in them, no session waited on another's
lock, the API's health probe stayed at p95 39 ms, and the longest transaction lasted 33 s. Work waits on the
nightly's run lock, held across arXiv and model batches, and that lock is A3's. A1 is groundwork:

- A2's workers share **one pool**. Today every request, loop and journal opens its own connection.
- A3's lanes run the owner's work alongside the nightly. That is safe only if every write is a **short unit**
  holding **per-paper locks in one order**. Today, dozens of places outside `store` open transactions or changes,
  locks are taken in twelve modules, and four session locks are held across network calls.
- A3's chunked re-settle after a taxonomy edit needs **settling to be cheap**. Today a settle reloads the taxonomy
  and every past verdict of the paper. A taxonomy edit settles the same paper again for each changed topic, up to
  6,777 papers in one transaction. At the October review's count of about 20 statements a settle, that is some
  135,000 statements, an extrapolation that has not been measured.

The one speed claim A1 makes is the settle. It is measured before and after: the 6,777-paper Field re-settled on a
copy of production.

## Units

**One pool per process.** Every database access goes through a unit the gate opens. Nothing outside the gate holds a
connection or sees `AsyncConnection`.

- **A read unit** reads one snapshot (repeatable read, read-only) and takes no advisory lock.
- **A write unit** names its locks when it opens (below), does its reads and writes, and commits.
- **Units are typed.** The gate hands out a `ReadUnit` or a `WriteUnit`. A Repository that only reads takes a
  `ReadUnit`, and a `WriteUnit` is accepted wherever a `ReadUnit` is. A Repository that writes takes only a
  `WriteUnit`. So the type checker (`ty`, in CI from A1 on) rejects a write inside a read unit, and Postgres
  rejects one at runtime too, since a read unit's transaction is read-only.
- **Units are short.** Fetching, parsing and model calls happen outside them (the architecture's "slow work outside,
  saving inside").

**Nesting is strict.** Opening a unit while another is open on the same task is an error. A function that works
inside a unit takes it as a parameter. The function at the boundary opens the unit, and only it commits. Nothing
joins an outer unit through a savepoint. So the signature shows what commits together:

- a function that takes a unit runs inside someone else's transaction and never commits;
- a function that takes none opens its own unit.

This matters for the rule that an event is written in the same transaction as its data, and for A2, where a
retried work item must not have half-committed. It also keeps the nesting check free of exceptions. It costs a split
in two for any Service used both on its own and inside another: one part takes the unit and does the work, and a thin
part opens the unit. No code today depends on a savepoint's partial rollback.

## Locks

**The gate owns every lock.** Each lock has a name, and the gate fixes the order: the taxonomy (shared, or exclusive
for an edit), then papers in id order, then the rest. A write unit takes its locks when it opens and refuses one
asked for later, as `change` does today. The exception is a unit holding the taxonomy lock exclusively: it may lock
papers as it finds them, because every other unit that locks papers takes the taxonomy lock first and so cannot be
holding any. No advisory-lock SQL remains outside the gate.

**Session locks held across network calls** move into the gate too, named. These are the run lock, the Deep
worker's, dispatch's and the schedule-kind lock. They are not units, and they break the architecture's rule that
only a unit holds a lock. The gate keeps them on an explicit list of locks held across calls, and that list may only
shrink. A2 turns their loops into Workers and A3 removes the run lock, which empties the list. Two other session
locks hold nothing across calls and are not on the list: retally's covers one update of a run's totals and becomes an
ordinary lock of a write unit; the migration lock is a startup lock, named in the gate.

## The nesting check

Two things raise in tests and development: opening a unit while another is open on the same task, and calling the
Model gate (today's model gateway) while a unit is open. In production either is logged as an error and work carries
on. A5's Fetch and Compute gates call the same check when they arrive. Holding a lock from the held-across-calls list
does not trigger it, because those locks are not units.

## Settling

**`change` becomes a Service over a write unit.** It opens the unit with the taxonomy and paper locks, runs the
block, settles each paper once and commits. The pipeline steps' `record` functions take the open unit, and their
callers open `change`.

**Settling gets cheap through its reads.** The rule itself stays a per-paper Policy in Python: the owner's word, the
effective grade, rarity, the passes a paper wants. What changes is how its inputs arrive:

- one taxonomy snapshot per unit, read when settling starts, so a unit that edits the taxonomy settles against
  the taxonomy as its edit left it;
- the papers' labels, reactions, weights and current verdicts (not their full history), read in one query each for
  all the papers in the unit;
- each paper settled once per unit, however many changed topics reach it.

Inputs the rule depends on, such as whether automatic Deep reads are on (`word.automatic_deep` today), are passed to
it rather than read from a module global. Moving the rule into set-based SQL is not part of A1. If the measurement
shows batched reads are not enough, the rule can move into SQL then.

## Folded in

These backlog items are under A1 in [`backlog.md`](../../backlog.md). They are stages after the gate, and A1 is done
when they are merged:

- the SQL outside `store`: the places listed in `tests/test_sql_home.py`, an empty list at the end;
- tests for `change` with several papers: overlapping sets in reverse order across two connections, and rollback
  when a later paper fails;
- `/summary` reworked, with the sidebar's count for today and today in the schedule's zone;
- Calibrate reading each topic's pool once;
- the smaller read gaps: unclassified papers listed, code and weights links from the analysis, the source of the
  owner's word from the API, and typed responses for `/quality`, `/runs` and `/calibrate`;
- intake no longer rewriting unchanged rows on its overlap;
- `store`'s listed import violations (`events`, `llm`) gone;
- the drops the owner marked in `notes/still-used.md`.

Ratings on the generated text move to A3, beside Calibrate's corrections. They need their own decision on the UI,
and they are not database work.

## Done when

- one pool per process, and typed units everywhere: nothing outside the gate holds a connection;
- every lock is named, ordered and taken through the gate. No advisory-lock SQL remains outside it, and the session
  locks are on the held-across-calls list;
- the nesting check runs, raising in tests and logging in production;
- settling batches its reads and settles each paper once per unit, measured before and after on the 6,777-paper
  Field;
- the folded items are merged, and the violation lists in A1's area are empty: the SQL outside `store`, `store`'s
  imports, and advisory locks outside the gate.

## Not in A1

- **The taxonomy edit's one exclusive transaction.** It gets cheaper here, but committing at once and re-settling in
  chunks is A3's.
- **The run lock and the waits behind it.** A3.
- **The session locks' loops.** The Deep worker, dispatch and the scheduler become Workers in A2. Until then they
  keep their session locks, named and listed.
- **Recording slow units and lock waits** on work records. A4.
- **Attribution** of each gate call to a work item or a request, and its check. A2.
- **Details for the plan:** pool sizes, lock names, the unit API's exact shape, the order of stages, and how the
  measurement is taken.
