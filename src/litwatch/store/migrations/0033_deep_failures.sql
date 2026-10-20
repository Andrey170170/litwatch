-- Deep's two kinds of failure (failure-handling plan, P3; docs/specs/v1/pipeline.md, section 6).
-- `failure` says whose failure ended an attempt without a card: the paper's, which counts as a
-- failed run, or the service's, which does not. `service_failed_at` is when litwatch last saw
-- the service fail the attempt (its job tagged `service`, an error submitting or following it),
-- ended or not: the Deep worker pauses its queue for a while after the latest.
ALTER TABLE deep_attempts
    ADD COLUMN failure text CHECK (failure IN ('paper', 'service')),
    ADD COLUMN service_failed_at timestamptz;

-- An attempt that ended without a card before this counted against its paper.
UPDATE deep_attempts SET failure = 'paper' WHERE ended_at IS NOT NULL AND error IS NOT NULL;

-- The daily cap on automatic cards is gone, and with it what counted them.
DROP INDEX deep_attempts_at_idx;
