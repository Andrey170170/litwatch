-- Topic re-runs (docs/plans/jobs.md, P2): a job of kind `rerun` grades its scope's papers for
-- one topic again, under a ceiling.
--
-- `ceiling` is the most the job may spend over its life, in USD (none for a Process job), and
-- `estimate` what its offer estimated; `wake_at` keeps a job whose passes all await their text
-- from being claimed again before then; `ladder` how far down its graders' lists each paper's
-- grading starts on its next try on the same text ({paper: {"revision": n, "runs": n,
-- "refused": [role]}}, models.md, D4), kept across Try again. A re-run's scope: {"topic",
-- "revision", "scopes", "since", "until", "papers": [id]}.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('process', 'rerun'));
ALTER TABLE jobs ADD COLUMN ceiling numeric;
ALTER TABLE jobs ADD COLUMN estimate numeric;
ALTER TABLE jobs ADD COLUMN wake_at timestamptz;
ALTER TABLE jobs ADD COLUMN ladder jsonb NOT NULL DEFAULT '{}';

-- The re-run whose grading queued a text pass works it, under its ceiling; no other worker
-- claims it while it does. Released (NULL) once the job ends.
ALTER TABLE fulltext_passes ADD COLUMN job_id bigint REFERENCES jobs (id);
CREATE INDEX fulltext_passes_job ON fulltext_passes (job_id) WHERE job_id IS NOT NULL;
