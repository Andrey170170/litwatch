# Pitfalls

Reviewed through: PR #64 (2026-10-15) · next ID: P14

- **P1 · Recovery and retry paths follow current policy, not fresh-work rules.** Retries, expired leases, reconciliation sweeps and owner run-now see current state (wanted, dismissed, priority) but no fresh-work cap; a retry resets every layer that failed; retry intent is stored, not inferred from execution state. Check: walk each new queue through retry, lost lease, recovery sweep and owner takeover. Does each one reach the work, and does a capped sweep reach its tail?
- **P2 · Combined data belongs to one paper revision.** A verdict, Weight, Brief or pass joined to a paper is from its current revision, unknown versions included; a frozen record (digest, eval run) reads only what it froze; a resume key covers every input. Check: in each join and each reuse, what happens if the paper was rewritten in between?
- **P3 · Check and act under one lock.** An eligibility read and the write it permits share one transaction holding the locks, taxonomy first and then papers; after awaited I/O (archive read, model call), recheck before spending or recording. Check: what can a dismissal, rewrite, pause or concurrent worker change between your check and your write?
- **P4 · One rule decides membership, scope and filters.** Parents count their descendants; keyword, uncertain and held paths count; every view of one selection (list, count, export, navigation, archive band) applies the same filters; SQL and `core` agree. Check: does the new query restate a rule that `core` or another surface already has?
- **P5 · Bounds cover the whole invocation.** `--limit`, daily caps, deadlines and timeouts apply in SQL before loading, across every phase and polling round, and to every blocking call, failure paths included. Check: can a loop, a second phase, an error retry or an unbounded HTTP call get past the bound?
- **P6 · Topic status and dismissal hold everywhere.** Paused, draft and unfollowed topics and dismissed papers get their specified treatment in every action, alert, status count and UI choice. Check: for each new selection or action, what happens to each topic status and to a dismissed paper?
- **P7 · A change settles what depends on it, in the same transaction.** A rewrite, class move, topic reorder or eligibility change re-derives grades, rarity, library, PDF policy and passes, and writes its outbox event, atomically. Check: what derived state reads the thing you changed, and who recomputes it if the process dies after commit?
- **P8 · Docs follow the interface.** `docs/integrations.md`, the READMEs, `deploy/README.md`, test comments and OpenAPI error responses match the changed behaviour. Check: grep the docs for every name and rule you changed.
- **P9 · Explicit configuration wins, and empty is not absent.** An explicit disable beats environment credentials; an empty section is not a disabled one; an optional feature needs no settings when off and refuses to load without its dependency. Check: try the feature off, empty, env-only and missing its dependency.
- **P10 · Usage counts every billed attempt.** Costs and run counts come from each attempt as it happens, killed and failed ones included, grouped by the configuration that ran and attributed by ID, not by overlapping timestamps. Check: does an interrupted or retried attempt still show in the totals?
- **P11 · Destructive scripts prove they own their target.** A script that drops or deletes matches the exact database name (after PostgreSQL truncates it) or normalized path it created, and keeps logs until its process is confirmed dead. Check: what is the worst existing thing the delete could match?
- **P12 · Edge inputs keep their meaning.** `0`, `false` and empty are values, not absence: no `x or None` or bare truthiness where they mean something; TeX matching strips `%` comments first, minding `\%` and backslash parity. Check: grep the diff for `or None`, truthiness on optional values, and TeX patterns run on raw source.

## Watching

- A failed web request renders as empty content, with no retry (PR #56).
- A refetch or invalidation reorders a list under the reader (PR #56).
- Per-paper or per-topic web state (a form draft, shelf expansion, a mutation's target) leaks across navigation (PR #56).
- Overlapping web writes finish out of order, or pending clears before the refresh lands (PR #56).
- A stack smoke test passes on a failure page or on fixture identity (PR #64).
- An eval report claims more than its labels or data show (PR #57).
