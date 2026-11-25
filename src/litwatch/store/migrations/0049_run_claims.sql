-- A run is alive by the claim it runs under (docs/plans/a2-work-gate.md, P3): `lease_token`
-- is the token of the work item's lease it was opened under, or its job's latest chunk's
-- (`store.work.link_run`). An item taken again gets a new token, so a run of a claim lost is
-- not revived by the next one. A job names its latest run (`run_id`), which tells a process
-- that kept another of its runs open that the job moved on without it.
ALTER TABLE runs ADD COLUMN lease_token uuid;
ALTER TABLE jobs ADD COLUMN run_id bigint REFERENCES runs (id);
CREATE INDEX work_items_leased ON work_items (lease_token) WHERE state = 'running';

-- The runs open now keep the claim they are alive by.
UPDATE runs r SET lease_token = w.lease_token
FROM work_items w
WHERE r.finished_at IS NULL AND w.state = 'running' AND (w.id = r.work_id OR w.run_id = r.id);
UPDATE jobs j SET run_id = (SELECT max(r.id) FROM runs r WHERE r.job_id = j.id)
WHERE j.state = 'running';
