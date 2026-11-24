"""The gates: one module per shared resource, the only door to it
(docs/specs/architecture.md, "Gates"). A gate imports `core` and nothing else of litwatch, but
for the Database gate and the Repository it keeps its own bookkeeping with (the Work gate's
`store.work`). `database` is the Database gate: the connection pool, units and every advisory
lock. `work` is the Work gate: the queue of work items, their leases and the Worker runner.
`context` carries what a gate call serves: the work item and the attribution."""
