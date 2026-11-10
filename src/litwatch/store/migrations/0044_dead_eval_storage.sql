-- Evaluation storage nothing writes or reads (A0, docs/notes/still-used.md): `regression` and
-- `eval_samples`, whose sets live in `evals/labels/`; the first v1's triage and grade columns,
-- which classification and the Fit replaced (0021); and `eval_runs.run_id`, the first of the
-- run records `run_ids` holds.
DROP TABLE regression;
DROP TABLE eval_samples;
ALTER TABLE eval_results DROP COLUMN triage_yes, DROP COLUMN triage_why, DROP COLUMN grade;
-- 0021 added `run_ids` empty, so a run recorded before it holds its run only in `run_id`.
UPDATE eval_runs SET run_ids = array_prepend(run_id, run_ids)
WHERE run_id IS NOT NULL AND array_position(run_ids, run_id) IS NULL;
ALTER TABLE eval_runs DROP COLUMN run_id;
