-- Replay (N2c; docs/specs/v1/feedback.md, evaluation): the labelled and missed papers through
-- the current classification, keyword check and grading, written to the evaluation tables only.
-- Additive: the first v1's triage columns stay, unused, as evaluation history.

-- What classification concluded for a paper and a Focus topic. `classifier`: the classifier
-- alone made it a member at threshold; `uncertain`: the uncertain band did; both NULL while
-- classification has not run or when it failed (`error`). `probability`: the highest the
-- classifier gave any class in the topic's subtree. `keyword` (the pattern the keyword check
-- matched) is kept from before. What grading concluded: `fit`, with `judge`, `check_opinion`
-- and `split` as before; NULL with `grade_error` when grading failed.
ALTER TABLE eval_results
    ADD COLUMN classifier   boolean,
    ADD COLUMN uncertain    boolean,
    ADD COLUMN probability  double precision,
    ADD COLUMN fit          text CHECK (fit IN ('central', 'related', 'adjacent', 'none'));

-- The labels a run was measured against, frozen when it started, so its numbers can be read
-- again however the label files or the misses change. A cohort carries its own positives; a
-- paper and topic can be in several cohorts.
CREATE TABLE eval_labels (
    eval_run_id  bigint NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    paper_id     text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic        text NOT NULL,
    cohort       text NOT NULL,     -- owner | provisional | legacy | silver | miss
    positive     boolean NOT NULL,  -- counts toward recall
    PRIMARY KEY (eval_run_id, cohort, paper_id, topic)
);

-- Every run record the evaluation ran under: a resumed replay opens another, and the cost is
-- their sum. `run_id` stays the first.
ALTER TABLE eval_runs ADD COLUMN run_ids bigint[] NOT NULL DEFAULT '{}';
