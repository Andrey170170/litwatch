"""The gates: one module per shared resource, the only door to it
(docs/specs/architecture.md, "Gates"). A gate imports `core` and nothing else of litwatch.
`database` is the Database gate: the connection pool, units and every advisory lock."""
