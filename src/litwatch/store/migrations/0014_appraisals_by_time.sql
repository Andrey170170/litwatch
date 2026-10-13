-- The newest appraisal is the one given last, not the one stored last: `db import-feedback`
-- copies appraisals from another database with their own times and new ids.
DROP INDEX appraisals_paper_idx;
CREATE INDEX appraisals_paper_idx ON appraisals (paper_id, at DESC, id DESC);
