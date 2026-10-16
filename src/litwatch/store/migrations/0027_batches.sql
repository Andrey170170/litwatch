-- Message Batches for the nightly run (docs/specs/v1/models.md, batches): a batched call's row is
-- written before its batch is sent and given the batch's id once the provider accepts it, so a
-- run started again attaches to the batch instead of paying for the call twice.
ALTER TABLE inference_calls ADD COLUMN batch_id text;
CREATE INDEX inference_calls_batched_idx ON inference_calls (request_key, id)
    WHERE batch_id IS NOT NULL AND output IS NULL AND error IS NULL;
