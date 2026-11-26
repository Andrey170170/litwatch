-- One Model gate per process (docs/plans/a2-work-gate.md, P4). A call sent under a ceiling keeps
-- what it reserved (`reserved`, USD), so a charge still unknown counts at its reservation in
-- what a job spent, across its runs and processes. A batch is settled once, by its id, and its
-- calls looked up by it. Both are additive: the image before ignores them.
ALTER TABLE inference_calls ADD COLUMN reserved double precision;
CREATE INDEX inference_calls_batch_idx ON inference_calls (batch_id) WHERE batch_id IS NOT NULL;
