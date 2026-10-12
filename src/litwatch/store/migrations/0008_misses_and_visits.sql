-- Reported misses, and when each topic page was last opened (so every device agrees on
-- what is new).

-- A paper the owner found elsewhere that litwatch did not surface, for one Watch topic. The
-- trace is an evaluation run of kind `miss`: what triage, the keyword audit and both graders
-- said about it, written to the evaluation tables only.
CREATE TABLE misses (
    id           bigserial PRIMARY KEY,
    paper_id     text NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
    topic        text NOT NULL,
    eval_run_id  bigint REFERENCES eval_runs (id) ON DELETE SET NULL,
    trace        text NOT NULL DEFAULT 'pending' CHECK (trace IN ('pending', 'done', 'failed')),
    trace_error  text,
    reported_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (paper_id, topic)
);

CREATE TABLE topic_visits (
    topic      text PRIMARY KEY,
    opened_at  timestamptz NOT NULL DEFAULT now()
);

-- When each entry joined its issue: a same-day amendment is new to a topic page opened
-- after the issue was first built.
ALTER TABLE issue_entries ADD COLUMN added_at timestamptz NOT NULL DEFAULT now();
