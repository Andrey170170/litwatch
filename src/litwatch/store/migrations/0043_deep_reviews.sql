-- A Deep read's reviews (docs/plans/deep-read.md, "Stored reviews"): each round's scores,
-- checks and findings, as the deep service returned them with the job, kept with the attempt
-- beside its trace and read through `litwatch deep trace`. NULL: no review ran (a Deep card
-- from before, or a job that ended before its first review).
ALTER TABLE deep_attempts ADD COLUMN reviews jsonb;
