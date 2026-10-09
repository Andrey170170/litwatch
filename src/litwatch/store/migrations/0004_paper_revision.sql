-- A paper's own count of rewrites. arXiv's version number is often unknown, so it cannot say
-- which text a candidate or verdict was about; this can.
ALTER TABLE papers ADD COLUMN revision integer NOT NULL DEFAULT 1;
ALTER TABLE candidates ADD COLUMN paper_revision integer NOT NULL DEFAULT 1;
ALTER TABLE verdicts ADD COLUMN paper_revision integer NOT NULL DEFAULT 1;
