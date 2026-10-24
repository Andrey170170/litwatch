-- Replays and digests written again as jobs (docs/plans/jobs.md, P3). A replay's scope:
-- {"eval_run": id}, the evaluation run whose labels it froze when it was made (`papers` their
-- papers); a digest's: {"digest": id}, the failed digest it writes again.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('process', 'rerun', 'replay', 'digest'));
-- One write job not finished per digest, so Write again asked again finds it.
CREATE UNIQUE INDEX jobs_one_digest ON jobs ((scope -> 'digest'))
    WHERE kind = 'digest' AND state NOT IN ('done', 'discarded');
