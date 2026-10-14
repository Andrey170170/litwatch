# Workflow profile

- **Shipping:** PR flow for every change.
- **Base branch: `v1`**, until v1 merges into `main` at the switch (milestone N8 in
  [milestones.md](../specs/v1/milestones.md)). Branch from `v1` and open PRs into it. `main` holds release 0.14,
  which runs in production; nothing goes into it before the switch. After the switch, `main` is the base again and
  this line goes.
- **Specs** live in `docs/specs/<version>/`, one file per subject with a `README.md` index; v1's is
  [`docs/specs/v1/`](../specs/v1/README.md), with the web app's design in its `DESIGN.md`. v1 is built as one hard
  change: `milestones.md` is its stage plan, and a deferred finding goes into the milestone that owns it.
