# Workflow profile

- **Shipping:** PR flow for every change.
- **Base branch: `main`**, which runs in production. Branch from it and open PRs into it.
- **Specs** live in `docs/specs/<version>/`, one file per subject with a `README.md` index; v1's is
  [`docs/specs/v1/`](../specs/v1/README.md), with the web app's design in its `DESIGN.md`. v1 is built as one hard
  change: `milestones.md` is its stage plan, and a deferred finding goes into the milestone that owns it.
