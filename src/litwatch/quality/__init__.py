"""The quality harness: what the models decide and write, scored against the labels in
`evals/labels/` (docs/agents/verification.md). Each cohort is scored on its own, never pooled.
Classification first (milestone N1); Gists, Briefs and digests join with their milestones."""
