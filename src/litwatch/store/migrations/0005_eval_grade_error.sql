-- Why forced grading produced no grade, apart from triage's error: a paper whose grading
-- failed must count as a miss, and one that was never sent to grading must not.
ALTER TABLE eval_results ADD COLUMN grade_error text;
