-- The first v1's pipeline goes (build plan, D8): triage, candidates, field memberships,
-- takes and their ratings, the writer's debts, issues and reviews. Classification, digests
-- and the new memberships arrive with their own migrations. `set_asides` stays for digests;
-- `papers.dismissed_at` and the correction columns stay for awaiting classification.

DROP TABLE take_ratings;
DROP TABLE issue_entries;
DROP TABLE issues;
DROP TABLE takes;
DROP TABLE due;
DROP TABLE review_picks;
DROP TABLE reviews;
DROP TABLE candidates;
DROP TABLE triage;
DROP TABLE memberships;

-- Only reviews read a topic's cadence.
ALTER TABLE topics DROP COLUMN cadence;

-- A miss's trace was a dry run through triage and grading; the miss itself stays.
ALTER TABLE misses
    DROP COLUMN eval_run_id,
    DROP COLUMN trace,
    DROP COLUMN trace_error,
    DROP COLUMN trace_started;
