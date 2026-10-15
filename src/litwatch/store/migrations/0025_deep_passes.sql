-- Deep card passes (N3; docs/specs/v1/pipeline.md, section 6, and integrations.md): a Deep card is
-- a full-text pass like a Brief, so it waits for the text in the queue instead of in
-- `deep_waiting`, and every submission is an attempt of a pass, with the job's usage.

-- One Deep pass open per paper. It is not bound to one revision: it reads the text the paper has
-- when it runs, and the card is labelled with the revision and arXiv version it read.
CREATE UNIQUE INDEX fulltext_passes_one_deep ON fulltext_passes (paper_id)
    WHERE state <> 'done' AND kind = 'deep';

-- An attempt is one submission to the deep service: for a pass, the job it started once
-- accepted, and when it ended. `usage` is the job's usage as the service reported it (turns,
-- input and output tokens, list cost); NULL on an ended attempt says it is unknown (the job was
-- lost before it reported, or the submission's answer never came), never zero.
ALTER TABLE deep_attempts
    ALTER COLUMN event_id DROP NOT NULL,
    ADD COLUMN pass_id bigint REFERENCES fulltext_passes (id) ON DELETE SET NULL,
    ADD COLUMN job_id text,
    ADD COLUMN ended_at timestamptz,
    ADD COLUMN error text,
    ADD COLUMN usage jsonb;
CREATE INDEX deep_attempts_pass_idx ON deep_attempts (pass_id);
-- An attempt of the first v1 ended with its event; its usage was never recorded.
UPDATE deep_attempts SET ended_at = at WHERE ended_at IS NULL;

-- The card says what it was written from: the paper revision, the arXiv version (NULL when the
-- archive could not say) and the source of the text, and the pass that wrote it. The job being
-- followed is the attempt's now.
ALTER TABLE deep_cards
    ADD COLUMN paper_revision integer,
    ADD COLUMN arxiv_version integer,
    ADD COLUMN text_source text CHECK (text_source IN ('latex', 'pdf')),
    ADD COLUMN pass_id bigint REFERENCES fulltext_passes (id) ON DELETE SET NULL,
    DROP COLUMN job_id,
    DROP COLUMN job_since,
    DROP COLUMN job_revision,
    DROP COLUMN job_version,
    DROP COLUMN job_event,
    DROP COLUMN job_automatic;

DROP TABLE deep_waiting;
